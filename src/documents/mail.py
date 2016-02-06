import datetime
import email
import imaplib
import os
import random
import re
import time

from base64 import b64decode
from dateutil import parser

from django.conf import settings

from .consumer import Consumer


class MailFetcherError(Exception):
    pass


class InvalidMessageError(Exception):
    pass


class Message(object):
    """
    A crude, but simple email message class.  We assume that there's a subject
    and n attachments, and that we don't care about the message body.
    """

    # This regex is probably more restrictive than it needs to be, but it's
    # better safe than sorry.
    SAFE_SUBJECT_REGEX = re.compile(r"^[\w\- ,.']+$")

    def _set_time(self, message):
        self.time = datetime.datetime.now()
        message_time = message.get("Date")
        if message_time:
            try:
                self.time = parser.parse(message_time)
            except (ValueError, AttributeError):
                pass  # We assume that "now" is ok

    def __init__(self, data):
        """
        Cribbed heavily from
        https://www.ianlewis.org/en/parsing-email-attachments-python
        """

        self.subject = None
        self.time = None
        self.attachment = None

        message = email.message_from_bytes(data)
        self.subject = message.get("Subject")

        self._set_time(message)

        if self.subject is None:
            raise InvalidMessageError("Message does not have a subject")
        if not self.SAFE_SUBJECT_REGEX.match(self.subject):
            raise InvalidMessageError("Message subject is unsafe")

        print('Fetching email: "{}"'.format(self.subject))

        attachments = []
        for part in message.walk():

            content_disposition = part.get("Content-Disposition")
            if not content_disposition:
                continue

            dispositions = content_disposition.strip().split(";")
            if not dispositions[0].lower() == "attachment":
                continue

            file_data = part.get_payload()

            attachments.append(Attachment(
                b64decode(file_data), content_type=part.get_content_type()))

        if len(attachments) == 0:
            raise InvalidMessageError(
                "There don't appear to be any attachments to this message")

        if len(attachments) > 1:
            raise InvalidMessageError(
                "There's more than one attachment to this message. It cannot "
                "be indexed automatically."
            )

        self.attachment = attachments[0]

    def __bool__(self):
        return bool(self.attachment)

    @property
    def file_name(self):

        prefix = str(random.randint(100000, 999999))
        if self.SAFE_SUBJECT_REGEX.match(self.subject):
            prefix = self.subject

        return "{}.{}".format(prefix, self.attachment.suffix)


class Attachment(object):

    SAFE_SUFFIX_REGEX = re.compile(
        r"^(application/(pdf))|(image/(png|jpeg|gif|tiff))$")

    def __init__(self, data, content_type):

        self.content_type = content_type
        self.data = data
        self.suffix = None

        m = self.SAFE_SUFFIX_REGEX.match(self.content_type)
        if not m:
            raise MailFetcherError(
                "Not-awesome file type: {}".format(self.content_type))
        self.suffix = m.group(2) or m.group(4)

    def read(self):
        return self.data


class MailFetcher(object):

    def __init__(self):

        self._connection = None
        self._host = settings.MAIL_CONSUMPTION["HOST"]
        self._port = settings.MAIL_CONSUMPTION["PORT"]
        self._username = settings.MAIL_CONSUMPTION["USERNAME"]
        self._password = settings.MAIL_CONSUMPTION["PASSWORD"]
        self._inbox = settings.MAIL_CONSUMPTION["INBOX"]

        self._enabled = bool(self._host)

        self.last_checked = datetime.datetime.now()

    def pull(self):
        """
        Fetch all available mail at the target address and store it locally in
        the consumption directory so that the file consumer can pick it up and
        do its thing.
        """

        if self._enabled:

            for message in self._get_messages():

                print("Storing email: \"{}\"".format(message.subject))

                t = int(time.mktime(message.time.timetuple()))
                file_name = os.path.join(Consumer.CONSUME, message.file_name)
                with open(file_name, "wb") as f:
                    f.write(message.attachment.data)
                    os.utime(file_name, times=(t, t))

        self.last_checked = datetime.datetime.now()

    def _get_messages(self):

        self._connect()
        self._login()

        r = []
        for message in self._fetch():
            if message:
                r.append(message)

        self._connection.expunge()
        self._connection.close()
        self._connection.logout()

        return r

    def _connect(self):
        self._connection = imaplib.IMAP4_SSL(self._host, self._port)

    def _login(self):

        login = self._connection.login(self._username, self._password)
        if not login[0] == "OK":
            raise MailFetcherError("Can't log into mail: {}".format(login[1]))

        inbox = self._connection.select("INBOX")
        if not inbox[0] == "OK":
            raise MailFetcherError("Can't find the inbox: {}".format(inbox[1]))

    def _fetch(self):

        for num in self._connection.search(None, "ALL")[1][0].split():

            __, data = self._connection.fetch(num, "(RFC822)")

            message = None
            try:
                message = Message(data[0][1])
            except InvalidMessageError as e:
                print(e)
                pass

            self._connection.store(num, "+FLAGS", "\\Deleted")
            if message:
                yield message
