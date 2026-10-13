import json
import re

from bs4 import BeautifulSoup

from studip_sync.logins import LoginBase, LoginError
from studip_sync.parsers import ParserError


class GeneralLogin(LoginBase):
    @staticmethod
    def name():
        return "General"

    @staticmethod
    def config_creator_get_auth_data():
        return {}

    @staticmethod
    def login(session, username, password, auth_type_data):
        with session.get(session.url.login_page()) as response:
            if not response.ok:
                raise LoginError("Cannot access Stud.IP login page")
            login_data = GeneralLogin.extract_login_data(response.text)

        login_params_auth = {
            "loginname": username,
            "password": password
        }

        login_params = {**login_params_auth, **login_data['params']}

        with session.post(login_data['action'], data=login_params) as response:
            if not response.ok:
                raise LoginError("Cannot post login data")
            elif "messagebox_error" in response.text:
                raise LoginError("Wrong credentials, cannot login")

            error = GeneralLogin.extract_login_error(response.text)
            if error:
                raise LoginError(error)

        # Test if logged in. Two changes from upstream:
        #
        # GET, not POST: Stud.IP runs POSTs through its CSRF check, which
        # bounces a request carrying no security_token back to the login page
        # even with a valid session — indistinguishable from a bad password.
        #
        # And judge by the landing URL, not the body: Göttingen answers a
        # rejected login with HTTP 200 and the login page, whose own nav
        # contains "Veranstaltungen", so upstream's body check always passed
        # and a wrong password surfaced later as "Could not find courses".
        # An anonymous request is redirected to /dispatch.php/login, the same
        # signal scripts/studip.py:is_wall uses.
        with session.get(session.url.studip_main()) as response:
            if not response.ok:
                raise LoginError("Cannot access Stud.IP main page")
            elif "/dispatch.php/login" in response.url:
                raise LoginError("Wrong credentials, cannot login")

    @staticmethod
    def extract_login_data(html):
        soup = BeautifulSoup(html, 'lxml')

        response = {}

        for form in soup.find_all('form'):
            # Göttingen's login page renders two language-selector forms ahead of
            # the login form, so "first form with an action" posts the
            # credentials to nobody_settings/store_settings. Require the
            # username field instead.
            if 'action' in form.attrs and form.find('input', attrs={"name": "loginname"}):
                response['action'] = form.attrs['action']
                response['params'] = {}

                # Everything the form carries except the credentials, which the
                # caller fills in. Upstream whitelisted four hidden fields and
                # looked at <input> only, which dropped the submit control —
                # Stud.IP's login handler is gated on it (Göttingen's is
                # <button name="Login">), so without it the POST is silently
                # ignored: the login page comes back unchanged, no error, same
                # login_ticket, and correct credentials look wrong.
                for field in form.find_all(['input', 'button']):
                    name = field.attrs.get('name')
                    if name and name not in ('loginname', 'password'):
                        response['params'][name] = field.attrs.get('value', '')

                return response

        raise ParserError("login: Couldn't find login data! Is the base url correct?")

    @staticmethod
    def extract_login_error(html):
        """The reason a login was refused, or None.

        Stud.IP reports it to its Vue notification manager rather than in the
        markup: a JSON island whose `notifications` carry type "exception" and
        a `details` string like
        "<b>standard:</b> Ungültige Benutzername/Passwort-Kombination!".
        """
        for island in re.findall(r'<script type="application/json">(.*?)</script>', html, re.S):
            if "SystemNotificationManager" not in island:
                continue

            try:
                data = json.loads(island)
            except ValueError:
                continue

            for notification in data.get("props", {}).get("notifications", []):
                if notification.get("type") != "exception":
                    continue

                detail = re.sub(r"<[^>]+>", " ", notification.get("details", ""))
                detail = re.sub(r"\s+", " ", detail).strip()
                return "{} {}".format(notification.get("message", ""), detail).strip()

        return None
