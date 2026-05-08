import json
import urllib.request

ENABLE_THRESHOLD = 70
DISABLE_THRESHOLD = 60

def percent_complete(locale):
    return ((locale['totalStrings'] - locale['missingStrings']) / locale['totalStrings']) * 100

fxa_locales = json.loads(urllib.request.urlopen("https://raw.githubusercontent.com/mozilla/fxa/main/libs/shared/l10n/src/lib/supported-languages.json").read().decode())

print("✅=enabled, ❌=disabled")

with urllib.request.urlopen("https://pontoon.mozilla.org/graphql?query={project(slug:%22mozilla-accounts%22){name,localizations{locale{code,name},totalStrings,missingStrings}}}") as url:
    data = json.loads(url.read().decode())
    for locale in sorted(data['data']['project']['localizations'], key = lambda x: x['locale']['code']):

        current_status = ""
        future_status = "⚪"
        percent = percent_complete(locale)

        if locale['locale']['code'] in fxa_locales:
            current_status = "✅"
            future_status = "✅" # assumes it stays the same
        else:
            current_status = "❌"
            future_status = "❌"

        if percent >= ENABLE_THRESHOLD:
            future_status = "✅"

        if percent <= DISABLE_THRESHOLD:
            future_status = "❌"

        if current_status == future_status:
            startbold = endbold = ""
        else:
            startbold = '\033[1m'
            endbold = '\033[0m'

        print(f"{startbold}{current_status} {future_status} {locale['locale']['name']} ({locale['locale']['code']}): {percent:.1f}%{endbold}")








