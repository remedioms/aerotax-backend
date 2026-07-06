# FR24-Harvester-Flotte (verteilte Positions-Ernte)

Schließt das China/Russland/Ozean-Coverage-Loch der freien ADS-B-Netze — mit
FR24 als Quelle, aber **verteilt über viele IPs**, damit kein Single-IP-Block
greift und alle Zonen parallel laufen.

```
 VM1 (IP A) TILES=0,1  ┐
 VM2 (IP B) TILES=2,3  ├─► Supabase fr24_live ─► AeroX-Backend liest warm
 VM3 (IP C) TILES=4,5  │        (kein eigener FR24-Kontakt mehr)
 VM4 (IP D) TILES=6,7  ┘
```

Jede VM pollt ihre Kacheln (`FR24_TILES` in `harvester.py`, identisch zum
Backend) und upsertet normalisierte Rows nach `fr24_live`. Das Backend
(`_fr24_warm_from_store`) liest die Tabelle alle 30 s in seinen In-Memory-Index;
solange der Store frisch ist (< 120 s), fasst das Backend FR24 selbst NICHT an.
Fällt die ganze Flotte aus, harvestet das Backend als Fallback selbst 1 Kachel/40 s.

## Kacheln (Index → Region)
`0` Zentralasien/West-China · `1` Trans-Sibirien · `2` Ost-China/Korea/Japan ·
`3` Naher Osten/Kaspisch · `4` Indien/Indik · `5` Nordatlantik ·
`6` Nordpazifik-West · `7` Afrika/Südatlantik

## Setup pro Oracle-Always-Free-VM (Ubuntu, ~5 Min)

Empfohlen: 4 VMs in **verschiedenen Oracle-Regionen** (z.B. Frankfurt, Amsterdam,
Ashburn, Singapur) → echte distinkte IPs. Jede VM bekommt 2 Kacheln.

```bash
sudo apt update && sudo apt install -y python3-pip
pip3 install requests

# harvester.py auf die VM kopieren (scp/git/curl raw), dann:
export SUPABASE_URL='https://<projectref>.supabase.co'
export SUPABASE_KEY='<SERVICE_ROLE_KEY>'      # Schreibrecht auf fr24_live
export TILES='0,1'                            # VM2: '2,3'  VM3: '4,5'  VM4: '6,7'
export POLL_SECONDS='20'                      # pro Kachel also alle 40 s
python3 harvester.py
```

Als Dienst (überlebt Reboot):

```ini
# /etc/systemd/system/fr24-harvester.service
[Unit]
Description=AeroX FR24 harvester
After=network-online.target

[Service]
Environment=SUPABASE_URL=https://<projectref>.supabase.co
Environment=SUPABASE_KEY=<SERVICE_ROLE_KEY>
Environment=TILES=0,1
Environment=POLL_SECONDS=20
ExecStart=/usr/bin/python3 /opt/fr24/harvester.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```
```bash
sudo mkdir -p /opt/fr24 && sudo cp harvester.py /opt/fr24/
sudo systemctl enable --now fr24-harvester
journalctl -u fr24-harvester -f      # Live-Logs: "tile0 rows=1191 upserted=1191"
```

## Verifikation
- VM-Logs zeigen `upserted=<n>` (n>0) → Ernte läuft.
- Supabase: `select count(*) from fr24_live where updated_at > now() - interval '2 minutes';`
  sollte je nach aktiver Luftfahrt einige Tausend sein.
- Backend-Logs: `[fr24] store_warm rows=… index=…` → Backend liest die Flotte.

## Hinweise
- FR24-ToS untersagt Scraping → **non-commercial-Grauzone**. Deshalb verteilt,
  höflich (1 Call/40 s pro IP) und nur als Coverage-Loch-Fallback.
- `POLL_SECONDS` nicht unter ~15 s drücken — sonst riskiert eine IP doch einen Block.
- Block-Symptom: `tile ERROR` / leere Antworten. Der Harvester wartet dann 30 s;
  hält es an, IP wechseln (andere Oracle-Region) oder Kachel auf andere VM ziehen.
- `SUPABASE_KEY` = Service-Role-Key. Nie ins Repo committen — nur als VM-Env/Secret.
