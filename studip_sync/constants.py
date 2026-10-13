from studip_sync.logins import LoginPreset
from studip_sync.logins.general import GeneralLogin
from studip_sync.logins.shibboleth import ShibbolethLogin

URL_BASEURL_DEFAULT = "https://studip.uni-goettingen.de"
CONFIG_FILENAME = "config.json"
LOGIN_PRESETS = [
    LoginPreset(name="University of Göttingen", base_url="https://studip.uni-goettingen.de",
                auth_type="general", auth_data={}),
    LoginPreset(name="University of Passau", base_url="https://studip.uni-passau.de/studip/",
                auth_type="shibboleth", auth_data={
            "login_url": "https://studip.uni-passau.de/studip/index.php?again=yes&sso=shib",
            "sso_post_url": "https://studip.uni-passau.de/Shibboleth.sso/SAML2/POST"
        }),
    LoginPreset(name="Trier University of Applied Sciences", base_url="https://studip.hochschule-trier.de",
                auth_type="shibboleth", auth_data={
            "login_url": "https://studip.hochschule-trier.de/index.php?again=yes&sso=shibldap",
            "sso_post_url": "https://studip.hochschule-trier.de/Shibboleth.sso/SAML2/POST"
        })
]
AUTHENTICATION_TYPES = {"general": GeneralLogin,
                        "shibboleth": ShibbolethLogin}
AUTHENTICATION_TYPE_DEFAULT = "general"
AUTHENTICATION_TYPE_DATA_DEFAULT = {}

# requests has no default timeout at all, so a connection the server accepts
# and then stops answering on hangs the sync for as long as the TCP stack
# allows — overnight, in the cron case, with the next run blocked behind it.
#
# From studip-sync/studip-sync#36, which uses a flat 30s for every request.
# Split in two here, because a flat timeout cannot fit both kinds of request
# this program makes: the read timeout is the wait for the *first* byte as
# well as between bytes, and Stud.IP zips a whole course server-side before
# answering a bulk download, which takes minutes for a large course. 30s would
# abort those. The streaming requests therefore get their own, generous limit.
HTTP_CONNECT_TIMEOUT = 15
HTTP_READ_TIMEOUT = 60
HTTP_DOWNLOAD_READ_TIMEOUT = 600
