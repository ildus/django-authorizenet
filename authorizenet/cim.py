import xml.dom.minidom
import urllib2
import re

from django.conf import settings
from authorizenet import AUTHNET_CIM_URL, AUTHNET_TEST_CIM_URL
from authorizenet.signals import *
from authorizenet.models import CIMResponse, Response


BILLING_FIELDS = ('firstName', 'lastName', 'company', 'address', 'city', 'state', 'zip', 'country', 'phoneNumber', 'faxNumber')
CREDIT_CARD_FIELDS = ('cardNumber', 'expirationDate', 'cardCode')


def extract_form_data(data):
    """
    Convert all keys in data dictionary from underscore_format to
    camelCaseFormat and return the new dict
    """
    to_upper = lambda match: match.group(1).upper()
    to_camel = lambda x: re.sub("_([a-z])", to_upper, x)
    return dict(map(lambda x: (to_camel(x[0]), x[1]), data.items()))


def create_form_data(data):
    """
    Convert all keys in data dictionary from camelCaseFormat to
    underscore_format and return the new dict
    """
    to_lower = lambda match: "_" + match.group(1).lower()
    to_under = lambda x: re.sub("([A-Z])", to_lower, x)
    return dict(map(lambda x: (to_under(x[0]), x[1]), data.items()))


def add_profile(customer_id, payment_form_data, billing_form_data):
    """
    Add a customer profile with a single payment profile and return a tuple of
    the CIMResponse, profile ID, and single-element list of payment profile IDs.

    Arguments:
    customer_id -- unique merchant-assigned customer identifier
    payment_form_data -- dictionary with keys in CREDIT_CARD_FIELDS
    billing_form_data -- dictionary with keys in BILLING_FIELDS
    """
    payment_data = extract_form_data(payment_form_data)
    billing_data = extract_form_data(billing_form_data)
    payment_data['expirationDate'] = payment_data['expirationDate'].strftime('%Y-%m')
    helper = CreateProfileRequest(customer_id, billing_data, payment_data)
    response = helper.get_response()
    if response.success:
        profile_id = helper.profile_id
        payment_profile_ids = helper.payment_profile_ids
        customer_was_created.send(sender=response,
                                  customer_id=helper.customer_id,
                                  profile_id=helper.profile_id,
                                  payment_profile_id=helper.payment_profile_id)
    else:
        profile_id = None
        payment_profile_ids = None
        customer_was_flagged.send(sender=response,
                                  customer_id=helper.customer_id)
    return response, profile_id, payment_profile_ids


def update_payment_profile(profile_id, payment_profile_id, payment_form_data, billing_form_data):
    """
    Update a customer payment profile and return the CIMResponse.

    Arguments:
    profile_id -- unique gateway-assigned profile identifier
    payment_profile_id -- unique gateway-assigned payment profile identifier
    payment_form_data -- dictionary with keys in CREDIT_CARD_FIELDS
    billing_form_data -- dictionary with keys in BILLING_FIELDS
    """
    payment_data = extract_form_data(payment_form_data)
    billing_data = extract_form_data(billing_form_data)
    payment_data['expirationDate'] = payment_data['expirationDate'].strftime('%Y-%m')
    helper = UpdatePaymentProfileRequest(profile_id, payment_profile_id, billing_data, payment_data)
    response = helper.get_response()
    return response


def create_payment_profile(profile_id, payment_form_data, billing_form_data):
    """
    Create a customer payment profile and return a tuple of the CIMResponse and
    payment profile ID.

    Arguments:
    profile_id -- unique gateway-assigned profile identifier
    payment_form_data -- dictionary with keys in CREDIT_CARD_FIELDS
    billing_form_data -- dictionary with keys in BILLING_FIELDS
    """
    payment_data = extract_form_data(payment_form_data)
    billing_data = extract_form_data(billing_form_data)
    payment_data['expirationDate'] = payment_data['expirationDate'].strftime('%Y-%m')
    helper = CreatePaymentProfileRequest(profile_id, billing_data, payment_data)
    response = helper.get_response()
    if response.success:
        payment_profile_id = helper.payment_profile_id
    else:
        payment_profile_id = None
    return response, payment_profile_id


def delete_payment_profile(profile_id, payment_profile_id):
    """
    Delete a customer payment profile and return the CIMResponse.

    Arguments:
    profile_id -- unique gateway-assigned profile identifier
    payment_profile_id -- unique gateway-assigned payment profile identifier
    """
    helper = DeletePaymentProfileRequest(profile_id, payment_profile_id)
    response = helper.get_response()
    return response


def get_profile(profile_id):
    """
    Retrieve a customer payment profile from the profile ID and return a tuple
    of the CIMResponse and a list of dictionaries containing data for each
    payment profile.

    Arguments:
    profile_id -- unique gateway-assigned profile identifier
    """
    helper = GetProfileRequest(profile_id)
    response = helper.get_response()
    return response, helper.payment_profiles


def process_transaction(*args, **kwargs):
    """
    Retrieve a customer payment profile from the profile ID and return a tuple
    of the CIMResponse and a list of dictionaries containing data for each
    payment profile.

    See CreateTransactionRequest.__init__ for arguments and keyword arguments.
    """
    helper = CreateTransactionRequest(*args, **kwargs)
    response = helper.get_response()
    if response.transaction_response:
        if response.transaction_response.is_approved:
            payment_was_successful.send(sender=response.transaction_response)
        else:
            payment_was_flagged.send(sender=response.transaction_response)
    return response


class BaseRequest(object):
    """
    Abstract class used by all CIM request types
    """

    def __init__(self, action):
        self.create_base_document(action)
        if settings.AUTHNET_DEBUG:
            self.endpoint = AUTHNET_TEST_CIM_URL
        else:
            self.endpoint = AUTHNET_CIM_URL

    def create_base_document(self, action):
        """
        Create base document and root node and store them in self.document
        and self.root respectively.  The root node is created based on the
        action parameter.  The required merchant authentication node is added
        to the document automatically.
        """
        doc = xml.dom.minidom.Document()
        namespace = "AnetApi/xml/v1/schema/AnetApiSchema.xsd"
        root = doc.createElementNS(namespace, action)
        root.setAttribute("xmlns", namespace)
        doc.appendChild(root)

        self.document = doc
        authentication = doc.createElement("merchantAuthentication")
        name = self.get_text_node("name", settings.AUTHNET_LOGIN_ID)
        key = self.get_text_node("transactionKey", settings.AUTHNET_TRANSACTION_KEY)
        authentication.appendChild(name)
        authentication.appendChild(key)
        root.appendChild(authentication)

        self.root = root

    def get_response(self):
        """
        Submit request to Authorize.NET CIM server and return the resulting
        CIMResponse
        """
        request = urllib2.Request(self.endpoint, self.document.toxml(), {'Content-Type': 'text/xml'})
        raw_response = urllib2.urlopen(request)
        response_xml = xml.dom.minidom.parse(raw_response)
        self.process_response(response_xml)
        return self.create_response_object()

    def get_text_node(self, node_name, text):
        """Create a text-only XML node called node_name with contents of text"""
        node = self.document.createElement(node_name)
        node.appendChild(self.document.createTextNode(str(text)))
        return node

    def create_response_object(self):
        return CIMResponse.objects.create(result=self.result,
                                          result_code=self.resultCode,
                                          result_text=self.resultText)

    def process_response(self, response):
        for e in response.childNodes[0].childNodes:
            if e.localName == 'messages':
                self.process_message_node(e)

    def process_message_node(self, node):
        for e in node.childNodes:
            if e.localName == 'resultCode':
                self.result = e.childNodes[0].nodeValue
            if e.localName == 'message':
                for f in e.childNodes:
                    if f.localName == 'code':
                        self.resultCode = f.childNodes[0].nodeValue
                    elif f.localName == 'text':
                        self.resultText = f.childNodes[0].nodeValue


class BasePaymentProfileRequest(BaseRequest):
    def get_payment_profile_node(self, billing_data, credit_card_data, node_name="paymentProfile"):
        payment_profile = self.document.createElement(node_name)

        if billing_data:
            bill_to = self.document.createElement("billTo")
            for key in BILLING_FIELDS:
                value = billing_data.get(key)
                if value is not None:
                    node = self.get_text_node(key, value)
                    bill_to.appendChild(node)
            payment_profile.appendChild(bill_to)

        payment = self.document.createElement("payment")
        credit_card = self.document.createElement("creditCard")
        for key in CREDIT_CARD_FIELDS:
            value = credit_card_data.get(key)
            if value is not None:
                node = self.get_text_node(key, value)
                credit_card.appendChild(node)
        payment.appendChild(credit_card)
        payment_profile.appendChild(payment)

        return payment_profile


class CreateProfileRequest(BasePaymentProfileRequest):
    def __init__(self, customer_id, billing_data=None, credit_card_data=None):
        super(CreateProfileRequest, self).__init__("createCustomerProfileRequest")
        self.customer_id = customer_id
        profile_node = self.get_profile_node()
        if credit_card_data:
            payment_profiles = self.get_payment_profile_node(billing_data, credit_card_data, "paymentProfiles")
            profile_node.appendChild(payment_profiles)
        self.root.appendChild(profile_node)

    def get_profile_node(self):
        profile = self.document.createElement("profile")
        id_node = self.get_text_node("merchantCustomerId", self.customer_id)
        profile.appendChild(id_node)
        return profile

    def process_response(self, response):
        self.profile_id = None
        self.payment_profile_id = None
        for e in response.childNodes[0].childNodes:
            if e.localName == 'messages':
                self.process_message_node(e)
            elif e.localName == 'customerProfileId':
                self.profile_id = e.childNodes[0].nodeValue
            elif e.localName == 'customerPaymentProfileIdList':
                self.payment_profile_ids = []
                for f in e.childNodes:
                    self.payment_profile_ids.append(f.childNodes[0].nodeValue)


class UpdatePaymentProfileRequest(BasePaymentProfileRequest):
    def __init__(self, profile_id, payment_profile_id, billing_data=None, credit_card_data=None):
        super(UpdatePaymentProfileRequest, self).__init__("updateCustomerPaymentProfileRequest")
        profile_id_node = self.get_text_node("customerProfileId", profile_id)
        payment_profile = self.get_payment_profile_node(billing_data, credit_card_data, "paymentProfile")
        payment_profile.appendChild(self.get_text_node("customerPaymentProfileId", payment_profile_id))
        self.root.appendChild(profile_id_node)
        self.root.appendChild(payment_profile)


class CreatePaymentProfileRequest(BasePaymentProfileRequest):
    def __init__(self, profile_id, billing_data=None, credit_card_data=None):
        super(CreatePaymentProfileRequest, self).__init__("createCustomerPaymentProfileRequest")
        profile_id_node = self.get_text_node("customerProfileId", profile_id)
        payment_profile = self.get_payment_profile_node(billing_data, credit_card_data, "paymentProfile")
        self.root.appendChild(profile_id_node)
        self.root.appendChild(payment_profile)

    def process_response(self, response):
        for e in response.childNodes[0].childNodes:
            if e.localName == 'messages':
                self.process_message_node(e)
            elif e.localName == 'customerPaymentProfileId':
                self.payment_profile_id = e.childNodes[0].nodeValue


class DeletePaymentProfileRequest(BasePaymentProfileRequest):
    def __init__(self, profile_id, payment_profile_id):
        super(DeletePaymentProfileRequest, self).__init__("deleteCustomerPaymentProfileRequest")
        profile_id_node = self.get_text_node("customerProfileId", profile_id)
        payment_profile_id_node = self.get_text_node("customerPaymentProfileId", payment_profile_id)
        self.root.appendChild(profile_id_node)
        self.root.appendChild(payment_profile_id_node)


class GetProfileRequest(BaseRequest):
    def __init__(self, profile_id):
        super(GetProfileRequest, self).__init__("getCustomerProfileRequest")
        profile_id_node = self.get_text_node("customerProfileId", profile_id)
        self.root.appendChild(profile_id_node)

    def process_children(self, node, field_list):
        child_dict = {}
        for e in node.childNodes:
            if e.localName in field_list:
                if e.childNodes:
                    child_dict[e.localName] = e.childNodes[0].nodeValue
                else:
                    child_dict[e.localName] = ""
        return child_dict

    def extract_billing_data(self, node):
        return create_form_data(self.process_children(node, BILLING_FIELDS))

    def extract_credit_card_data(self, node):
        return create_form_data(self.process_children(node, CREDIT_CARD_FIELDS))

    def extract_payment_profiles_data(self, node):
        data = {}
        for e in node.childNodes:
            if e.localName == 'billTo':
                data['billing'] = self.extract_billing_data(e)
            if e.localName == 'payment':
                data['credit_card'] = self.extract_credit_card_data(e.childNodes[0])
            if e.localName == 'customerPaymentProfileId':
                data['payment_profile_id'] = e.childNodes[0].nodeValue
        return data

    def process_response(self, response):
        self.payment_profiles = []
        for e in response.childNodes[0].childNodes:
            if e.localName == 'messages':
                self.process_message_node(e)
            if e.localName == 'profile':
                for f in e.childNodes:
                    if f.localName == 'paymentProfiles':
                        self.payment_profiles.append(self.extract_payment_profiles_data(f))


class CreateTransactionRequest(BaseRequest):
    def __init__(self, profile_id, payment_profile_id, transaction_type, amount, transaction_id=None, delimiter=None):
        """
        Arguments:
        profile_id -- unique gateway-assigned profile identifier
        payment_profile_id -- unique gateway-assigned payment profile identifier
        transaction_type -- One of the transaction types listed below.
        amount -- Dollar amount of transaction

        Keyword Arguments:
        transaction_id -- Required by PriorAuthCapture, Refund, and Void transactions
        delimiter -- Delimiter used for transaction response data

        Accepted transaction types:
        AuthOnly, AuthCapture, CaptureOnly, PriorAuthCapture, Refund, Void
        """
        super(CreateTransactionRequest, self).__init__(
                "createCustomerProfileTransactionRequest")
        self.profile_id = profile_id
        self.payment_profile_id = payment_profile_id
        self.transaction_type = transaction_type
        self.amount = amount
        self.transaction_id = transaction_id
        if delimiter:
            self.delimiter = delimiter
        else:
            self.delimiter = getattr(settings, 'AUTHNET_DELIM_CHAR', "|")
        self.add_transaction_node()
        self.add_extra_options()

    def add_transaction_node(self):
        transaction_node = self.document.createElement("transaction")
        type_node = self.document.createElement("profileTrans%s" % self.transaction_type)

        amount_node = self.get_text_node("amount", self.amount)
        type_node.appendChild(amount_node)
        transaction_node.appendChild(type_node)
        self.add_profile_ids(type_node)
        if self.transaction_id:
            trans_id_node = self.get_text_node("transId", self.transaction_id)
            type_node.appendChild(trans_id_node)
        self.root.appendChild(transaction_node)

    def add_profile_ids(self, transaction_type_node):
        profile_node = self.get_text_node("customerProfileId", self.profile_id)
        transaction_type_node.appendChild(profile_node)

        payment_profile_node = self.get_text_node("customerPaymentProfileId", self.payment_profile_id)
        transaction_type_node.appendChild(payment_profile_node)

    def add_extra_options(self):
        extra_options_node = self.get_text_node("extraOptions", "x_delim_data=TRUE&x_delim_char=%s" % self.delimiter)
        self.root.appendChild(extra_options_node)

    def create_response_object(self):
        try:
            response = Response.objects.create_from_list(self.transaction_result)
        except AttributeError:
            response = None
        return CIMResponse.objects.create(result=self.result,
                                          result_code=self.resultCode,
                                          result_text=self.resultText,
                                          transaction_response=response)

    def process_response(self, response):
        for e in response.childNodes[0].childNodes:
            if e.localName == 'messages':
                self.process_message_node(e)
            if e.localName == 'directResponse':
                self.transaction_result = e.childNodes[0].nodeValue.split(self.delimiter)
