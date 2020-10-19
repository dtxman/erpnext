# -*- coding: utf-8 -*-
# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
import frappe
import six
from datetime import datetime
from frappe.model.document import Document
from frappe.email import sendmail_to_system_managers
from frappe.utils import add_days, add_years, nowdate, getdate, add_months, get_link_to_form
from erpnext.non_profit.doctype.member.member import create_member
from frappe import _
import erpnext


class Membership(Document):
	def validate(self):
		if not self.member or not frappe.db.exists("Member", self.member):
			member_name = frappe.get_value('Member', dict(email=frappe.session.user))

			if not member_name:
				user = frappe.get_doc('User', frappe.session.user)
				member = frappe.get_doc(dict(
					doctype='Member',
					email=frappe.session.user,
					membership_type=self.membership_type,
					member_name=user.get_fullname()
				)).insert(ignore_permissions=True)
				member_name = member.name

			if self.get("__islocal"):
				self.member = member_name

		# get last membership (if active)
		last_membership = erpnext.get_last_membership()

		# if person applied for offline membership
		if last_membership and not frappe.session.user == "Administrator":
			# if last membership does not expire in 30 days, then do not allow to renew
			if getdate(add_days(last_membership.to_date, -30)) > getdate(nowdate()) :
				frappe.throw(_('You can only renew if your membership expires within 30 days'))

			self.from_date = add_days(last_membership.to_date, 1)
		elif frappe.session.user == "Administrator":
			self.from_date = self.from_date
		else:
			self.from_date = nowdate()

		if frappe.db.get_single_value("Membership Settings", "billing_cycle") == "Yearly":
			self.to_date = add_years(self.from_date, 1)
		else:
			self.to_date = add_months(self.from_date, 1)

	def on_payment_authorized(self, status_changed_to=None):
		if status_changed_to in ("Completed", "Authorized"):
			self.load_from_db()
			self.db_set('paid', 1)

	def generate_invoice(self, save=True):
		if not (self.paid or self.currency or self.amount):
			frappe.throw(_("The payment for this membership is not paid. To generate invoice fill the payment details"))

		if self.invoice:
			frappe.throw(_("An invoice is already linked to this document"))

		member = frappe.get_doc("Member", self.member)
		plan = frappe.get_doc("Membership Type", self.membership_type)
		settings = frappe.get_doc("Membership Settings")

		if not member.customer:
			frappe.throw(_("No customer linked to member {}", [member.name]))

		if not settings.debit_account:
			frappe.throw(_("You need to set <b>Debit Account</b> in Membership Settings"))

		if not settings.company:
			frappe.throw(_("You need to set <b>Default Company</b> for invoicing in Membership Settings"))

		invoice = make_invoice(self, member, plan, settings)
		self.invoice = invoice.name

		if save:
			self.save()

		return invoice

	def send_acknowlement(self):
		settings = frappe.get_doc("Membership Settings")
		if not settings.send_email:
			frappe.throw(_("You need to enable <b>Send Acknowledge Email</b> in Membership Settings"))

		member = frappe.get_doc("Member", self.member)
		plan = frappe.get_doc("Membership Type", self.membership_type)
		email = member.email_id if member.email_id else member.email
		attachments = [frappe.attach_print("Membership", self.name, print_format=settings.membership_print_format)]

		if self.invoice and settings.send_invoice:
			attachments.append(frappe.attach_print("Sales Invoice", self.invoice, print_format=settings.inv_print_format))

		email_template = frappe.get_doc("Email Template", settings.email_template)
		context = { "doc": self, "member": member}

		email_args = {
			"recipients": [email],
			"message": frappe.render_template(email_template.get("response"), context),
			"subject": frappe.render_template(email_template.get("subject"), context),
			"attachments": attachments,
			"reference_doctype": self.doctype,
			"reference_name": self.name
		}

		if not frappe.flags.in_test:
			frappe.enqueue(method=frappe.sendmail, queue='short', timeout=300, is_async=True, **email_args)
		else:
			frappe.sendmail(**email_args)

	def generate_and_send_invoice(self):
		invoice = self.generate_invoice(False)
		self.send_acknowlement()

def make_invoice(membership, member, plan, settings):
	invoice = frappe.get_doc({
		'doctype': 'Sales Invoice',
		'customer': member.customer,
		'debit_to': settings.debit_account,
		'currency': membership.currency,
		'is_pos': 0,
		'items': [
			{
				'item_code': plan.linked_item,
				'rate': membership.amount,
				'qty': 1
			}
		]
	})

	invoice.insert(ignore_permissions=True)
	invoice.submit()

	return invoice

def get_member_based_on_subscription(subscription_id, email):
	members = frappe.get_all("Member", filters={
					'subscription_id': subscription_id,
					'email_id': email
				}, order_by="creation desc")

	try:
		return frappe.get_doc("Member", members[0]['name'])
	except:
		return None

def verify_signature(data):
	signature = frappe.request.headers.get('X-Razorpay-Signature')

	settings = frappe.get_doc("Membership Settings")
	key = settings.get_webhook_secret()

	controller = frappe.get_doc("Razorpay Settings")

	controller.verify_signature(data, signature, key)

@frappe.whitelist(allow_guest=True)
def trigger_razorpay_subscription(*args, **kwargs):
	data = frappe.request.get_data(as_text=True)
	try:
		verify_signature(data)
	except Exception as e:
		log = frappe.log_error(e, "Webhook Verification Error")
		notify_failure(log)
		return { 'status': 'Failed', 'reason': e}

	if isinstance(data, six.string_types):
		data = json.loads(data)
	data = frappe._dict(data)

	subscription = data.payload.get("subscription", {}).get('entity', {})
	subscription = frappe._dict(subscription)

	payment = data.payload.get("payment", {}).get('entity', {})
	payment = frappe._dict(payment)

	try:
		if not data.event == "subscription.charged":
			return

		member = get_member_based_on_subscription(subscription.id, payment.email)
		if not member:
			member = create_member(frappe._dict({
				'fullname': payment.email,
				'email': payment.email,
				'plan_id': get_plan_from_razorpay_id(subscription.plan_id)
			}))

			member.subscription_id = subscription.id
			member.customer_id = payment.customer_id
			if subscription.notes and type(subscription.notes) == dict:
				notes = '\n'.join("{}: {}".format(k, v) for k, v in subscription.notes.items())
				member.add_comment("Comment", notes)
			elif subscription.notes and type(subscription.notes) == str:
				member.add_comment("Comment", subscription.notes)


		# Update Membership
		membership = frappe.new_doc("Membership")
		membership.update({
			"member": member.name,
			"membership_status": "Current",
			"membership_type": member.membership_type,
			"currency": "INR",
			"paid": 1,
			"payment_id": payment.id,
			"from_date": datetime.fromtimestamp(subscription.current_start),
			"to_date": datetime.fromtimestamp(subscription.current_end),
			"amount": payment.amount / 100 # Convert to rupees from paise
		})
		membership.insert(ignore_permissions=True)

		# Update membership values
		member.subscription_start = datetime.fromtimestamp(subscription.start_at)
		member.subscription_end = datetime.fromtimestamp(subscription.end_at)
		member.subscription_activated = 1
		member.save(ignore_permissions=True)
	except Exception as e:
		message = "{0}\n\n{1}\n\n{2}: {3}".format(e, frappe.get_traceback(), __("Payment ID"), payment.id)
		log = frappe.log_error(message, _("Error creating membership entry for {0}").format(member.name))
		notify_failure(log)
		return { 'status': 'Failed', 'reason': e}

	return { 'status': 'Success' }


def notify_failure(log):
	try:
		content = """Dear System Manager,
Razorpay webhook for creating renewing membership subscription failed due to some reason. Please check the following error log linked below

Error Log: {0}

Regards,
Administrator""".format(get_link_to_form("Error Log", log.name))
		sendmail_to_system_managers("[Important] [ERPNext] Razorpay membership webhook failed , please check.", content)
	except:
		pass

def get_plan_from_razorpay_id(plan_id):
	plan = frappe.get_all("Membership Type", filters={'razorpay_plan_id': plan_id}, order_by="creation desc")

	try:
		return plan[0]['name']
	except:
		return None
