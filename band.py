import os
from datetime import datetime
import re

from zeep import Client
from zeep.plugins import HistoryPlugin
from lxml import etree

# --- Django setup (kad veiktų paleidžiant tiesiog python band.py) ---
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from django.conf import settings

API_KEY = getattr(settings, "OPTIMUM_API_KEY", None)
WSDL = "http://api.optimum.lt/v1/lt/Trd.asmx?WSDL"  # svarbu: HTTP, nes per HTTPS pas tave buvo sertifikato mismatch

if not API_KEY:
    raise SystemExit(
        "Nerastas OPTIMUM_API_KEY. Įdėk į .env ir į settings.py pridėk OPTIMUM_API_KEY = os.getenv('OPTIMUM_API_KEY')"
    )

history = HistoryPlugin()
client = Client(WSDL, plugins=[history])
SOAP_HDR = {"Header": {"Key": API_KEY}}


def _xml_bytes_to_text(xml_bytes: bytes) -> str:
    try:
        root = etree.fromstring(xml_bytes)
        return etree.tostring(root, pretty_print=True, encoding="unicode")
    except Exception:
        try:
            return xml_bytes.decode("utf-8", errors="replace")
        except Exception:
            return str(xml_bytes)


def _mask_api_key(xml_text: str) -> str:
    return re.sub(r"(<Key>)(.*?)(</Key>)", r"\1***\3", xml_text, flags=re.DOTALL | re.IGNORECASE)


def dump_last_soap_exchange(prefix: str = "soap") -> None:
    sent = getattr(history, "last_sent", None)
    received = getattr(history, "last_received", None)

    sent_env = sent.get("envelope") if isinstance(sent, dict) else None
    recv_env = received.get("envelope") if isinstance(received, dict) else None

    if sent_env is None:
        print("[SOAP] Nėra last_sent envelope įrašo.")
        return

    req_text = _mask_api_key(_xml_bytes_to_text(etree.tostring(sent_env)))
    with open(f"{prefix}_request.xml", "w", encoding="utf-8") as f:
        f.write(req_text)

    if recv_env is not None:
        resp_text = _xml_bytes_to_text(etree.tostring(recv_env))
        with open(f"{prefix}_response.xml", "w", encoding="utf-8") as f:
            f.write(resp_text)

    print(f"[SOAP] Išsaugota: {prefix}_request.xml ir {prefix}_response.xml (API raktas užmaskuotas request'e).")


def list_operations() -> list[str]:
    ops: list[str] = []
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            ops.extend(list(port.binding._operations.keys()))
    return sorted(set(ops))


# 1) Greitas testas ar raktas veikia
resp = client.service.Hello(_soapheaders=SOAP_HDR)
print("HELLO:", resp)

# 2) Klientų sąrašas (kad patvirtintume, jog ryšys ok)
companies_resp = client.service.GetCompanies(datetime(2000, 1, 1), _soapheaders=SOAP_HDR)
if getattr(companies_resp, "Error", None):
    raise SystemExit(f"GetCompanies klaida: {companies_resp.Error}")

companies = list(getattr(companies_resp.Result, "Company", []) or [])
print(f"Rasta klientų: {len(companies)}")

# 3) Operacijų sąrašas – pas tave GetTransactions neegzistuoja šiame WSDL
ops = list_operations()
print("\n=== WSDL operations ({} vnt.) ===".format(len(ops)))
for name in ops:
    print(name)

# 4) Kandidatai pagal raktinius žodžius (dažniausiai TransactionId būna per TrnType/DocType/Settings)
keywords = ["trn", "transaction", "doc", "type", "invoice", "settings"]
print("\n=== Kandidatai pagal keyword ===")
for name in ops:
    low = name.lower()
    if any(k in low for k in keywords):
        print(name)

# 5) GetSettings – dažnai čia būna default reikšmės (tarp jų ir DflTrnTypeId, jei Optimum ją turi)
try:
    settings_resp = client.service.GetSettings(_soapheaders=SOAP_HDR)
    if getattr(settings_resp, "Error", None):
        print("GetSettings klaida:", settings_resp.Error)
    else:
        s = settings_resp.Result
        print("\n=== GetSettings (santrauka) ===")
        for attr in [
            "DflStrFllCode",
            "DflCstGrpFllCode",
            "DflSplGrpFllCode",
            "DflTrnTypeId",
            "DflDlvConditionId",
            "DflTrpTypeId",
        ]:
            if hasattr(s, attr):
                print(f"{attr}: {getattr(s, attr)}")
except Exception as e:
    print("GetSettings iškvietimas nepavyko:", e)

print("\nDONE. Iš šito sąrašo parinksim teisingą operaciją, kuri grąžina TransactionId/TrnType.")