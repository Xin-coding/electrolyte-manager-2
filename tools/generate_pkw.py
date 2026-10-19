"""Build-only: IAPWS water density and ionization constant at 0.101325 MPa."""
import json
from pathlib import Path
from iapws import IAPWS95
from iapws._iapws import _Kw

values = [_Kw(IAPWS95(T=t+273.15, P=.101325).rho, t+273.15) for t in range(100)]
path = Path(__file__).resolve().parents[1]/"em2/resources/pkw.json"
path.write_text(json.dumps({"source": "IAPWS ionization release; iapws 1.5.5 _Kw, IAPWS95 density",
                           "pressure_MPa": .101325, "temperature_C": "0..99, step 1",
                           "pkw": values}, indent=2), encoding="utf-8")
print(path)
