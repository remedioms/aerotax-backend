#!/usr/bin/env python3
"""Seedet das per-Airline Crew-Hotel-Verzeichnis mit der offiziellen Lufthansa-
Standardliste (FRAL/OF-SK Standard-Fahrzeitenliste Crewhotel) als
airline='LUFTHANSA', status='approved'.

Idempotent: löscht zuerst die bestehenden approved-LH-Seed-Zeilen (suggested_by
IS NULL = Seed-Herkunft) und schreibt sie neu. Crew-Vorschläge/-Korrekturen
(suggested_by gesetzt) bleiben unberührt.

Lauf auf dem Hetzner-Host (SUPABASE_URL / SUPABASE_SERVICE_KEY aus /opt/aerox env):
    docker exec -i aerotax-backend python3 - < scripts/seed_crew_hotel_directory.py
"""
import os
import sys

# LH-Standardliste — 1:1 aus dem iOS-Bundle (CrewHotelDirectory.swift `raw`).
# Format: IATA|BASE|HOTEL|MINUTES  (BASE leer = alle Bases, MINUTES 0 = fußläufig)
_RAW = """
ABV||Transcorp Hilton Abuja|50
AGP||NH Málaga|20
ALA||Mercure Almaty City Center|30
AMM||Grand Hyatt Amman|40
AMS||Leonardo Rembrandtpark|25
ARN||Elite Hotel Academia Uppsala|35
ATH||Divani Apollon Palace|30
ATL||Embassy Suites by Hilton Atlanta Buckhead|30
AUS||Renaissance Austin Hotel|40
BAH||ART Hotel & Resort|25
BCN||Labtwentytwo Barcelona, a Tribute Portfolio Hotel|25
BCN||NH Collection Constanza|25
BEG||Radisson Collection Old Mill|20
BER||Pullman Berlin Schweizerhof|45
BEY||Mövenpick Hotel & Resort Beirut|25
BGO||Radisson Blu Royal Hotel Bergen|30
BHX||Hilton Garden Inn Brindleyplace|40
BIO||Barceló Bilbao Nervión|20
BKK||Marriott Marquis Bangkok|45
BLL||Zleep Hotel Vejle|35
BLQ||Mercure Bologna Centro|40
BLR||Shangri-La Bengaluru|40
BLR||Conrad Bengaluru|40
BOG||Hilton Bogota|35
BOM||ITC Maratha, a Luxury Collection Hotel|15
BOS||Hyatt Regency Boston|20
BRE||Dorint City-Hotel Bremen|15
BUD||Intercity Budapest|35
CAI||Renaissance Cairo Mirage City Hotel|25
CDG||Pullman La Defense|60
CGN||Maritim Köln|25
CLT||Hilton Charlotte Uptown|30
CPH||Scandic Copenhagen|40
CPH||Scandic Falkoner|45
CPT||Radisson RED V&A Waterfront|35
CTA||NH Catania Centro|25
DEL||Hyatt Regency New Delhi|40
DEN||Hilton City Center|35
DFW||Renaissance Dallas Addison|30
DMM||Dana Rayhaan by Rotana|25
DRS||Maritim Hotel Dresden|25
DTW||The Henry Detroit|20
DUB||Clayton Hotel Ballsbridge|30
DUS||NH City Nord|20
DUS||Clayton Hotel Düsseldorf (ehem. Nikko)|30
DXB||Hilton Al Habtoor City|25
EDI||Courtyard by Marriott Edinburgh|40
EVN||Armenia Marriott Hotel Yerevan|30
EWR||The Westin Jersey City|40
EZE||Emperador Buenos Aires|50
FCO||NH Villa Carpegna|30
FRA||Sheraton Frankfurt Airport Hotel|0
GIG||Hilton Copacabana Hotel|40
GLA||Courtyard by Marriott Glasgow SEC|25
GOT||Gothia Towers|25
GRU||Hilton Morumbi|90
GRZ||Intercity Hotel Graz|20
GYD||Hilton Baku|30
HAJ||Sheraton Hannover Pelikan Hotel|30
HAM||Radisson Blu Hamburg|25
HEL||Clarion Hotel Helsinki|35
HKG||Harbour Grand Kowloon|40
HND|FRA|Keio Plaza Hotel|30
HND|MUC|Grand Nikko Odaiba|15
HYD||Radisson Blu Plaza Hyderabad|70
IAD||The Arlo Washington DC|45
IAH||JW Marriott Houston by The Galleria|40
ICN||Novotel Ambassador Yongsan - Dragon City|50
IKA||REXAN Hotel Airport|10
IST||Renaissance Polat Istanbul|70
JFK||Club Quarters World Trade Center|70
JNB|FRA|Marriott Melrose Arch|50
JNB|FRA|African Pride|50
JNB|MUC|The Maslow|50
KIX||New Otani Osaka|70
KRK||Holiday Inn Krakow City Centre|25
KWI||Radisson Blu Hotel Kuwait|20
LAD||InterContinental Miramar Luanda|30
LAX||DoubleTree by Hilton Hotel Torrance|30
LAX||JW Marriott Anaheim Resort|75
LCA||Radisson Blu Larnaca|20
LEJ||Adina Appartment Hotel Leipzig|30
LHR||Meliá White House|70
LIN||AC by Marriott|25
LIS||Altis Grand|15
LOS||Lagos Marriott Hotel Ikeja|25
LYS||Radisson Blu Lyon|45
MAA||The Westin Chennai|25
MAD||NH Collection Eurobuilding|15
MAD||NH Collection Abascal|25
MAN||AC Manchester City Centre|30
MEX||Hyatt Regency Mexico City|70
MIA|MUC|RIU Plaza Miami Beach|45
MIA|FRA|Residence Inn Sunny Isles|60
MLA||Corinthia St George's Bay|25
MRS||NH Collection Marseille|25
MSP||Marriott City Center|20
MUC||Munich Airport Marriott Hotel Freising|25
MUC||Leonardo Hotel Munich City Olympiapark|45
MXP||Starhotels Grand Milan Saronno|30
NAP||Royal Continental Hotel|25
NBO||Crowne Plaza Nairobi Airport (Lazizi)|15
NCE||Mercure Notre Dame|20
NGO||The Nagoya Hilton|60
NKG||InterContinental Nanjing|45
NQZ||Sheraton Astana|30
NUE||NH Collection Nürnberg City|20
OPO||Holiday Inn Gaia Hotel Porto|25
ORD||W Lakeshore|60
OSL||Radisson Blu Scandinavia Oslo|50
OTP||Sheraton Bucharest|35
PEK||Kempinski Beijing|45
PHC||Heliconia Park and Suites Ltd.|55
PHL||Sheraton Philadelphia Downtown|30
PRG||Vienna House Diplomat|30
PVG||Amara Signature Hotel|75
PVG||Grand Millennium Shanghai HongQiao|75
RDU||Durham Marriott City Center|30
RIX||Radisson Blu Elizabete|25
SAN||Carté Hotel Curio Collection by Hilton|20
SEA||Westin Bellevue|35
SEA||W Bellevue|35
SFO||Hilton San Francisco Union Square|25
SHE||Le Meridien Shenyang, Heping|40
SIN|FRA|Carlton Hotel Singapore|45
SIN|MUC|Carlton City Hotel Singapore|25
SJJ||Courtyard by Marriott|15
SJO||Crowne Plaza Corobicí|60
SKP||Holiday Inn Skopje|35
SOF||Grand Hotel Millennium Sofia|30
SSG||Sofitel Malabo Sipopo Le Golf|35
STL||Marriott Grand|30
STR||Pullman Stuttgart Fontana|15
SVG||Scandic Stavanger City|20
SZG||Dorint City-Hotel Salzburg|20
TAO||InterContinental Qingdao|80
TBS||Mercure Tbilisi Old Town|30
TIA||Hilton Garden Inn|45
TLL||Swissôtel Tallinn|15
TLS||Mercure Saint George|40
TLV||David Intercontinental Tel Aviv|30
TSR||NH Timisoara|25
VCE||Leonardo Royal Hotel Venice Mestre|15
VLC||NH Valencia Center|20
VNO||Radisson Blu Hotel Lietuva|20
WAW||Mercure Grand Hotel|20
YUL||Delta Hotels by Marriott Montreal|45
YVR||Hilton Vancouver Metrotown|45
YYZ||Chelsea Hotel Toronto|60
ZAG||Sheraton Zagreb|30
ZRH||Hyatt Place The Circle Zürich Airport|0
"""


def _parse_rows():
    rows = []
    for line in _RAW.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        iata, base, hotel, mins = (p.strip() for p in parts)
        if not iata or not hotel or not mins.isdigit():
            continue
        rows.append({
            "airline": "LUFTHANSA",
            "iata": iata.upper(),
            "base": base.upper() or None,
            "hotel": hotel,
            "transfer_min": int(mins),
            "status": "approved",
            "suggested_by": None,   # Seed-Herkunft = kein Vorschlagender
            "votes": 1,
            "active": True,
        })
    return rows


def main():
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("[seed] SUPABASE_URL/SUPABASE_SERVICE_KEY fehlen", file=sys.stderr)
        return 1
    from supabase import create_client
    sb = create_client(url, key)
    rows = _parse_rows()
    # Idempotent: alte Seed-Zeilen (approved LH, suggested_by IS NULL) weg, dann neu.
    sb.table("crew_hotel_directory").delete().eq(
        "airline", "LUFTHANSA").eq("status", "approved").is_(
        "suggested_by", "null").execute()
    for i in range(0, len(rows), 200):
        sb.table("crew_hotel_directory").insert(rows[i:i + 200]).execute()
    print(f"[seed] {len(rows)} Lufthansa-Crewhotels approved geseedet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
