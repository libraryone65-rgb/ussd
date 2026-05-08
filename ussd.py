from flask import Flask, request, Response, jsonify
from sqlalchemy import create_engine, Column, Integer, String, Text, Enum, DECIMAL, ForeignKey, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import os
import uuid
import requests
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')  # e.g. mysql+pymysql://user:pass@host/dbname
PAYSTACK_SECRET_KEY = os.getenv('PAYSTACK_SECRET_KEY')
PAYSTACK_INIT_URL = 'https://api.paystack.co/transaction/initialize'
PAYSTACK_VERIFY_URL = 'https://api.paystack.co/transaction/verify/'
PRICE_PER_VOTE_NGN = int(os.getenv('PRICE_PER_VOTE_NGN', '100'))

app = Flask(__name__)

if not DATABASE_URL:
	raise RuntimeError('Please set DATABASE_URL environment variable')

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class USSDResponse:
	def __init__(self, session_id=None, user_id=None, msisdn=None, message=None, continue_session=False):
		self.sessionID = session_id
		self.userID = user_id
		self.msisdn = msisdn
		# Arkesel expects `message` and `continueSession` booleans
		self.message = message
		self.continueSession = continue_session


class Nominee(Base):
	__tablename__ = 'nominees'
	id = Column(Integer, primary_key=True)
	nominee_code = Column(String(50), unique=True, nullable=False)
	full_name = Column(String(100), nullable=False)
	stage_name = Column(String(100), nullable=False)
	gender = Column(Enum('Male', 'Female', 'Other'), nullable=False)
	category = Column(String(100), nullable=True, default='General')
	photo = Column(String(255), nullable=True)
	bio = Column(Text, nullable=True)
	vote_count = Column(Integer, nullable=False, default=0)
	total_amount_raised = Column(DECIMAL(10,2), nullable=False, default=0.00)
	status = Column(Enum('active','inactive'), nullable=False, default='active')


class Vote(Base):
	__tablename__ = 'votes'
	id = Column(Integer, primary_key=True)
	nominee_id = Column(Integer, ForeignKey('nominees.id', ondelete='CASCADE'))
	voter_phone = Column(String(20), nullable=False)
	voter_email = Column(String(255), nullable=True)
	vote_count = Column(Integer, nullable=False, default=1)
	amount_paid = Column(DECIMAL(10,2), nullable=False, default=1.00)
	transaction_id = Column(String(100), nullable=True)
	reference = Column(String(100), nullable=True)
	status = Column(Enum('pending','completed','failed'), nullable=False, default='pending')
	payment_method = Column(String(50), nullable=True)


class VoteSession(Base):
	__tablename__ = 'vote_sessions'
	id = Column(Integer, primary_key=True)
	nominee_id = Column(Integer, ForeignKey('nominees.id', ondelete='CASCADE'))
	session_token = Column(String(100), unique=True, nullable=False)
	voter_phone = Column(String(20), nullable=False)
	voter_email = Column(String(255), nullable=True)
	vote_count = Column(Integer, nullable=False, default=1)
	amount = Column(DECIMAL(10,2), nullable=False, default=1.00)
	status = Column(Enum('pending','processing','completed','cancelled'), nullable=False, default='pending')
	created_at = Column(DateTime, server_default=func.now())


def format_amount_ngn(amount_int):
	return f"₦{amount_int:,}"


def ussd_response(text, end=False):
	prefix = 'END ' if end else 'CON '
	return Response(prefix + text, mimetype='text/plain')


@app.route('/ussd', methods=['POST'])
def ussd():
	data = request.get_json() if request.is_json else request.form.to_dict()
	# detect Arkesel-style payloads which include sessionID/msisdn/userData
	if isinstance(data, dict) and ('sessionID' in data or 'msisdn' in data) and 'userData' in data:
		session_id = data.get('sessionID')
		user_id = data.get('userID')
		msisdn = data.get('msisdn')
		new_session = data.get('newSession', False)
		user_data = (data.get('userData') or '').strip()

		db = SessionLocal()
		try:
			# Normalize input parts: remove trailing # and split
			user_data = user_data.replace('#', '')
			parts = user_data.split('*') if user_data != '' else ['']
			# Remove any leading non-numeric parts (dial codes)
			while parts and not parts[0].isdigit():
				parts.pop(0)

			# Start session: show nominees
			if new_session or parts == [''] or parts == []:
				nominees = db.query(Nominee).filter(Nominee.status == 'active').order_by(Nominee.vote_count.desc()).limit(10).all()
				if not nominees:
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='No nominees available at the moment. Thank you', continue_session=False)
					return jsonify(vars(resp))
				lines = ['Welcome to USSD Voting. Choose nominee:']
				for i, n in enumerate(nominees, start=1):
					lines.append(f"{i}. {n.stage_name} ({n.nominee_code})")
				lines.append('Reply with the number (e.g., 1) or nominee code')
				resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='\n'.join(lines), continue_session=True)
				return jsonify(vars(resp))

			# User selected nominee by number or code
			sel = parts[0].strip() if parts else ''
			nominees = db.query(Nominee).filter(Nominee.status == 'active').order_by(Nominee.vote_count.desc()).limit(10).all()
			nominee = None
			if sel.isdigit():
				idx = int(sel) - 1
				if 0 <= idx < len(nominees):
					nominee = nominees[idx]
			else:
				nominee = db.query(Nominee).filter(Nominee.nominee_code == sel).first()

			if nominee is None:
				resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Invalid nominee selection. Session ended.', continue_session=False)
				return jsonify(vars(resp))

			# If only nominee selected, ask for number of votes
			if len(parts) == 1:
				resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message=f"Selected: {nominee.stage_name}. Enter number of votes (e.g., 1)", continue_session=True)
				return jsonify(vars(resp))

			# Next part should be vote count
			if len(parts) == 2:
				try:
					vote_count = int(parts[1])
					if vote_count <= 0:
						raise ValueError()
				except Exception:
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Invalid vote count. Session ended.', continue_session=False)
					return jsonify(vars(resp))
				amount = vote_count * PRICE_PER_VOTE_NGN
				lines = [f"You are voting for {nominee.stage_name} x{vote_count}", f"Amount: {format_amount_ngn(amount)}", "1. Pay Now", "2. Cancel"]
				resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='\n'.join(lines), continue_session=True)
				return jsonify(vars(resp))

			# Confirmation selection
			if len(parts) >= 3:
				action = parts[2].strip()
				if action != '1':
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Payment cancelled. Thank you.', continue_session=False)
					return jsonify(vars(resp))
				# proceed to create session and initialize payment
				try:
					vote_count = int(parts[1])
				except Exception:
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Invalid vote count. Session ended.', continue_session=False)
					return jsonify(vars(resp))
				amount = vote_count * PRICE_PER_VOTE_NGN
				token = str(uuid.uuid4())
				# create vote_session and a pending vote record
				vs = VoteSession(nominee_id=nominee.id, session_token=token, voter_phone=msisdn or 'unknown', vote_count=vote_count, amount=amount, status='pending')
				db.add(vs)
				db.flush()
				# create Vote record (pending)
				reference = str(uuid.uuid4()).replace('-', '')[:32]
				vote = Vote(nominee_id=nominee.id, voter_phone=msisdn or 'unknown', voter_email=None, vote_count=vote_count, amount_paid=amount, reference=reference, status='pending')
				db.add(vote)
				db.commit()

				# Initialize Paystack transaction
				headers = {'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}', 'Content-Type': 'application/json'}
				payload = {
					'email': (msisdn or 'unknown') + '@noemail.local',
					'amount': int(amount) * 100,  # kobo
					'reference': reference,
					'callback_url': os.getenv('PAYSTACK_CALLBACK_URL', '')
				}
				if not PAYSTACK_SECRET_KEY:
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Payment could not be initiated (server not configured).', continue_session=False)
					return jsonify(vars(resp))
				resp_http = requests.post(PAYSTACK_INIT_URL, json=payload, headers=headers, timeout=15)
				if resp_http.status_code != 200:
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Payment initialization failed. Try again later.', continue_session=False)
					return jsonify(vars(resp))
				jr = resp_http.json()
				if not jr.get('status'):
					resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message='Payment initialization failed. Try again later.', continue_session=False)
					return jsonify(vars(resp))
				auth_url = jr['data'].get('authorization_url')
				message = 'Payment initiated. ' + ('Open this link to pay: ' + auth_url if auth_url else 'You will receive payment instructions via SMS.')
				resp = USSDResponse(session_id=session_id, user_id=user_id, msisdn=msisdn, message=message, continue_session=False)
				return jsonify(vars(resp))
		finally:
			db.close()

	# Fallback for other USSD providers: return plain text CON/END
	data = request.get_json() if request.is_json else request.form.to_dict()
	session_id = data.get('sessionId') or data.get('session_id')
	phone = data.get('phoneNumber') or data.get('msisdn') or data.get('phone') or data.get('source')
	text = data.get('text', '')

	db = SessionLocal()
	try:
		parts = text.split('*') if text is not None else ['']
		# Level 0: show list of nominees
		if text == '' or parts == ['']:
			nominees = db.query(Nominee).filter(Nominee.status == 'active').order_by(Nominee.vote_count.desc()).limit(10).all()
			if not nominees:
				return ussd_response('No nominees available at the moment. Thank you.', end=True)
			lines = ['Welcome to USSD Voting. Choose nominee:']
			for i, n in enumerate(nominees, start=1):
				lines.append(f"{i}. {n.stage_name} ({n.nominee_code})")
			lines.append('\nReply with the number (e.g., 1) or nominee code')
			return ussd_response('\n'.join(lines))

		# remaining original flow unchanged for non-Arkesel providers
		sel = parts[0].strip()
		nominees = db.query(Nominee).filter(Nominee.status == 'active').order_by(Nominee.vote_count.desc()).limit(10).all()
		nominee = None
		if sel.isdigit():
			idx = int(sel) - 1
			if 0 <= idx < len(nominees):
				nominee = nominees[idx]
		else:
			nominee = db.query(Nominee).filter(Nominee.nominee_code == sel).first()

		if nominee is None:
			return ussd_response('Invalid nominee selection. Session ended.', end=True)

		if len(parts) == 1:
			return ussd_response(f"Selected: {nominee.stage_name}. Enter number of votes (e.g., 1)")

		if len(parts) == 2:
			try:
				vote_count = int(parts[1])
				if vote_count <= 0:
					raise ValueError()
			except Exception:
				return ussd_response('Invalid vote count. Session ended.', end=True)
			amount = vote_count * PRICE_PER_VOTE_NGN
			lines = [f"You are voting for {nominee.stage_name} x{vote_count}", f"Amount: {format_amount_ngn(amount)}", "1. Pay Now", "2. Cancel"]
			return ussd_response('\n'.join(lines))

		if len(parts) >= 3:
			action = parts[2].strip()
			if action != '1':
				return ussd_response('Payment cancelled. Thank you.', end=True)
			try:
				vote_count = int(parts[1])
			except Exception:
				return ussd_response('Invalid vote count. Session ended.', end=True)
			amount = vote_count * PRICE_PER_VOTE_NGN
			token = str(uuid.uuid4())
			vs = VoteSession(nominee_id=nominee.id, session_token=token, voter_phone=phone or 'unknown', vote_count=vote_count, amount=amount, status='pending')
			db.add(vs)
			db.flush()
			reference = str(uuid.uuid4()).replace('-', '')[:32]
			vote = Vote(nominee_id=nominee.id, voter_phone=phone or 'unknown', voter_email=None, vote_count=vote_count, amount_paid=amount, reference=reference, status='pending')
			db.add(vote)
			db.commit()

			headers = {'Authorization': f'Bearer {PAYSTACK_SECRET_KEY}', 'Content-Type': 'application/json'}
			payload = {
				'email': (phone or 'unknown') + '@noemail.local',
				'amount': int(amount) * 100,  # kobo
				'reference': reference,
				'callback_url': os.getenv('PAYSTACK_CALLBACK_URL', '')
			}
			if not PAYSTACK_SECRET_KEY:
				return ussd_response('Payment could not be initiated (server not configured).', end=True)
			resp = requests.post(PAYSTACK_INIT_URL, json=payload, headers=headers, timeout=15)
			if resp.status_code != 200:
				return ussd_response('Payment initialization failed. Try again later.', end=True)
			jr = resp.json()
			if not jr.get('status'):
				return ussd_response('Payment initialization failed. Try again later.', end=True)
			auth_url = jr['data'].get('authorization_url')
			message = 'Payment initiated. ' \
					  + ('Open this link to pay: ' + auth_url if auth_url else 'You will receive payment instructions via SMS.')
			return ussd_response(message, end=True)
	finally:
		db.close()


@app.route('/paystack/webhook', methods=['POST'])
def paystack_webhook():
	# Recommended to verify signature in production
	event = request.get_json()
	if not event:
		return jsonify({'status': False}), 400
	# Handle charge.success (transaction completed)
	try:
		if event.get('event') == 'charge.success' or event.get('event') == 'transaction.completed':
			data = event.get('data', {})
			reference = data.get('reference')
			status = data.get('status')
			db = SessionLocal()
			try:
				vote = db.query(Vote).filter(Vote.reference == reference).first()
				if not vote:
					return jsonify({'status': False, 'message': 'vote not found'}), 404
				if status in ('success', 'completed'):
					vote.status = 'completed'
					vote.transaction_id = data.get('id')
					db.commit()
					return jsonify({'status': True}), 200
			finally:
				db.close()
	except Exception:
		pass
	return jsonify({'status': True}), 200


if __name__ == '__main__':
	app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))

