import json

import requests
from django.http import HttpResponse
from django.utils.dateparse import parse_datetime

from .base import AnymailBaseWebhookView
from ..exceptions import AnymailWebhookValidationFailure
from ..signals import tracking, AnymailTrackingEvent, EventType, RejectReason
from ..utils import get_anymail_setting, getfirst, combine


class AmazonSESBaseWebhookView(AnymailBaseWebhookView):
    """Base view class for Amazon SES webhooks (SNS Notifications)"""

    esp_name = "Amazon SES"

    def __init__(self, **kwargs):
        # whether to automatically respond to SNS SubscriptionConfirmation requests; default True
        # (Future: could also take a TopicArn or list to auto-confirm)
        self.auto_confirm_enabled = get_anymail_setting(
            "auto_confirm_sns_subscriptions", esp_name=self.esp_name, kwargs=kwargs, default=True)
        super(AmazonSESBaseWebhookView, self).__init__(**kwargs)

    @staticmethod
    def _parse_sns_message(request):
        # cache so we don't have to parse the json multiple times
        if not hasattr(request, '_sns_message'):
            try:
                body = request.body.decode(request.encoding or 'utf-8')
                request._sns_message = json.loads(body)
            except (TypeError, ValueError, UnicodeDecodeError) as err:
                raise AnymailWebhookValidationFailure("Malformed SNS message body %r" % request.body,
                                                      raised_from=err)
        return request._sns_message

    def validate_request(self, request):
        # Block random posts that don't even have matching SNS headers
        sns_message = self._parse_sns_message(request)
        header_type = request.META.get("HTTP_X_AMZ_SNS_MESSAGE_TYPE", "<<missing>>")
        body_type = sns_message.get("Type", "<<missing>>")
        if header_type != body_type:
            raise AnymailWebhookValidationFailure(
                'SNS header "x-amz-sns-message-type: %s" doesn\'t match body "Type": "%s"'
                % (header_type, body_type))

        if header_type not in ["Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"]:
            raise AnymailWebhookValidationFailure("Unknown SNS message type '%s'" % header_type)

        header_id = request.META.get("HTTP_X_AMZ_SNS_MESSAGE_ID", "<<missing>>")
        body_id = sns_message.get("MessageId", "<<missing>>")
        if header_id != body_id:
            raise AnymailWebhookValidationFailure(
                'SNS header "x-amz-sns-message-id: %s" doesn\'t match body "MessageId": "%s"'
                % (header_id, body_id))

        # TODO: Verify SNS message signature
        # https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.verify.signature.html
        # Requires ability to public-key-decrypt signature with Amazon-supplied X.509 cert
        # (which isn't in Python standard lib; need pyopenssl or pycryptodome, e.g.)

    def post(self, request, *args, **kwargs):
        # request has *not* yet been validated at this point
        if self.basic_auth and not request.META.get("HTTP_AUTHORIZATION"):
            # Amazon SNS requires a proper 401 response before it will attempt to send basic auth
            response = HttpResponse(status=401)
            response["WWW-Authenticate"] = 'Basic realm="Anymail WEBHOOK_SECRET"'
            return response
        return super(AmazonSESBaseWebhookView, self).post(request, *args, **kwargs)

    def parse_events(self, request):
        # request *has* been validated by now
        events = []
        sns_message = self._parse_sns_message(request)
        sns_type = sns_message.get("Type")
        if sns_type == "Notification":
            message_string = sns_message.get("Message")
            try:
                ses_event = json.loads(message_string)
            except (TypeError, ValueError):
                if message_string == "Successfully validated SNS topic for Amazon SES event publishing.":
                    pass  # this Notification is generated after SubscriptionConfirmation
                else:
                    raise AnymailWebhookValidationFailure("Unparsable SNS Message %r" % message_string)
            else:
                events = self.esp_to_anymail_events(ses_event, sns_message)
        elif sns_type == "SubscriptionConfirmation":
            self.auto_confirm_sns_subscription(sns_message)
        # else: just ignore other SNS messages (e.g., "UnsubscribeConfirmation")
        return events

    def esp_to_anymail_events(self, ses_event, sns_message):
        raise NotImplementedError()

    def auto_confirm_sns_subscription(self, sns_message):
        """Automatically accept a subscription to Amazon SNS topics, if the request is expected.

        If an SNS SubscriptionConfirmation arrives with HTTP basic auth proving it is meant for us,
        automatically load the SubscribeURL to confirm the subscription.
        """
        if not self.auto_confirm_enabled:
            return

        if not self.basic_auth:
            # Note: basic_auth (shared secret) confirms the notification was meant for us.
            # If WEBHOOK_SECRET isn't set, Anymail logs a warning but allows the request.
            # (Also, verifying the SNS message signature would be insufficient here:
            # if someone else tried to point their own SNS topic at our webhook url,
            # SNS would send a SubscriptionConfirmation with a valid Amazon signature.)
            raise AnymailWebhookValidationFailure(
                "Anymail received an unexpected SubscriptionConfirmation request for Amazon SNS topic "
                "'{topic_arn!s}'. (Anymail can automatically confirm SNS subscriptions if you set a "
                "WEBHOOK_SECRET and use that in your SNS notification url. Or you can manually confirm "
                "this subscription in the SNS dashboard with token '{token!s}'.)"
                "".format(topic_arn=sns_message.get('TopicArn'), token=sns_message.get('Token')))

        # WEBHOOK_SECRET *is* set, so the request's basic auth has been verified by now (in run_validators)
        response = requests.get(sns_message["SubscribeURL"])
        if not response.ok:
            raise AnymailWebhookValidationFailure(
                "Anymail received a {status_code} error trying to automatically confirm a subscription "
                "to Amazon SNS topic '{topic_arn!s}'. The response was '{text!s}'."
                "".format(status_code=response.status_code, text=response.text,
                          topic_arn=sns_message.get('TopicArn')))


class AmazonSESTrackingWebhookView(AmazonSESBaseWebhookView):
    """Handler for Amazon SES tracking notifications"""

    signal = tracking

    def esp_to_anymail_events(self, ses_event, sns_message):
        # Amazon SES has two notification formats, which are almost exactly the same:
        # - https://docs.aws.amazon.com/ses/latest/DeveloperGuide/event-publishing-retrieving-sns-contents.html
        # - https://docs.aws.amazon.com/ses/latest/DeveloperGuide/notification-contents.html
        # This code should handle either.
        event_id = sns_message.get("MessageId")  # unique to the SNS notification
        try:
            timestamp = parse_datetime(sns_message["Timestamp"])
        except (KeyError, ValueError):
            timestamp = None

        mail_object = ses_event.get("mail", {})
        message_id = mail_object.get("messageId")  # same as MessageId in SendRawEmail response
        all_recipients = mail_object.get("destination", [])

        # Recover tags and metadata from custom headers
        metadata = {}
        tags = []
        for header in mail_object.get("headers", []):
            name = header["name"].lower()
            if name == "x-tag":
                tags.append(header["value"])
            elif name == "x-metadata":
                try:
                    metadata = json.loads(header["value"])
                except (ValueError, TypeError, KeyError):
                    pass

        common_props = dict(  # AnymailTrackingEvent props for all recipients
            esp_event=ses_event,
            event_id=event_id,
            message_id=message_id,
            metadata=metadata,
            tags=tags,
            timestamp=timestamp,
        )
        per_recipient_props = [  # generate individual events for each of these
            dict(recipient=email_address)
            for email_address in all_recipients
        ]

        ses_event_type = getfirst(ses_event, ["eventType", "notificationType"], "<<type missing>>")
        event_object = ses_event.get(ses_event_type.lower(), {})  # e.g., ses_event["bounce"]

        if ses_event_type == "Bounce":
            common_props.update(
                event_type=EventType.BOUNCED,
                description="{bounceType}: {bounceSubType}".format(**event_object),
                reject_reason=RejectReason.BOUNCED,
            )
            per_recipient_props = [dict(
                recipient=recipient["emailAddress"],
                mta_response=recipient.get("diagnosticCode"),
            ) for recipient in event_object["bouncedRecipients"]]
        elif ses_event_type == "Complaint":
            common_props.update(
                event_type=EventType.COMPLAINED,
                description=event_object.get("complaintFeedbackType"),
                reject_reason=RejectReason.SPAM,
                user_agent=event_object.get("userAgent"),
            )
            per_recipient_props = [dict(
                recipient=recipient["emailAddress"],
            ) for recipient in event_object["complainedRecipients"]]
        elif ses_event_type == "Delivery":
            common_props.update(
                event_type=EventType.DELIVERED,
                mta_response=event_object.get("smtpResponse"),
            )
            per_recipient_props = [dict(
                recipient=recipient,
            ) for recipient in event_object["recipients"]]
        elif ses_event_type == "Send":
            common_props.update(
                event_type=EventType.SENT,
            )
        elif ses_event_type == "Reject":
            common_props.update(
                event_type=EventType.REJECTED,
                description=event_object["reason"],
                reject_reason=RejectReason.BLOCKED,
            )
        elif ses_event_type == "Open":
            # SES doesn't report which recipient opened the message (it doesn't
            # track them separately), so just report it for all_recipients
            common_props.update(
                event_type=EventType.OPENED,
                user_agent=event_object.get("userAgent"),
            )
        elif ses_event_type == "Click":
            # SES doesn't report which recipient clicked the message (it doesn't
            # track them separately), so just report it for all_recipients
            common_props.update(
                event_type=EventType.CLICKED,
                user_agent=event_object.get("userAgent"),
                click_url=event_object.get("link"),
            )
        elif ses_event_type == "Rendering Failure":
            event_object = ses_event["failure"]  # rather than ses_event["rendering failure"]
            common_props.update(
                event_type=EventType.FAILED,
                description=event_object["errorMessage"],
            )
        else:
            # Umm... new event type?
            common_props.update(
                event_type=EventType.UNKNOWN,
                description="Unknown SES eventType '%s'" % ses_event_type,
            )

        return [
            # AnymailTrackingEvent(**common_props, **recipient_props)  # Python 3.5+ (PEP-448 syntax)
            AnymailTrackingEvent(**combine(common_props, recipient_props))
            for recipient_props in per_recipient_props
        ]
