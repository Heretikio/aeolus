import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from config import Config
from store import Store

# Fixed instants (example TZ America/Chicago, CDT in July = UTC-5)
DAY = datetime(2026, 7, 3, 18, 0, tzinfo=timezone.utc)     # 13:00 local
NIGHT = datetime(2026, 7, 4, 4, 0, tzinfo=timezone.utc)    # 23:00 local, quiet
MORNING = datetime(2026, 7, 4, 13, 0, tzinfo=timezone.utc)  # 08:00 local, after quiet


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "aeolus.db"))


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    s.ensure_schema()
    s.seed_default_location(cfg.location_name, cfg.lat, cfg.lon)
    return s


class Recorder:
    """Stand-in for the Slack sender: records (text, at_channel) tuples."""

    def __init__(self):
        self.sent = []

    def __call__(self, text, at_channel=False):
        self.sent.append((text, at_channel))
        return True


@pytest.fixture
def sent():
    return Recorder()


def make_feature(msg_id="urn:oid:2.49.0.1.840.0.100", event="Tornado Warning",
                 vtec="/O.NEW.KOUN.TO.W.0032.260703T1730Z-260703T1900Z/",
                 severity="Extreme", certainty="Observed", urgency="Immediate",
                 message_type="Alert", references=None, expired_references=None,
                 geometry=None, ugc=("OKC027",), sent="2026-07-03T12:15:00-05:00",
                 onset="2026-07-03T12:30:00-05:00", ends="2026-07-03T14:00:00-05:00",
                 expires="2026-07-03T14:00:00-05:00", headline="Tornado Warning until 2 PM",
                 instruction="Take cover now.", extra_params=None):
    params = {}
    if vtec:
        params["VTEC"] = [vtec]
    if expired_references:
        params["expiredReferences"] = expired_references
    if extra_params:
        params.update(extra_params)
    return {
        "geometry": geometry,
        "properties": {
            "id": msg_id,
            "event": event,
            "messageType": message_type,
            "references": references if references is not None else [],
            "severity": severity,
            "certainty": certainty,
            "urgency": urgency,
            "response": "Shelter",
            "headline": headline,
            "description": "A confirmed tornado was located near your location.",
            "instruction": instruction,
            "sent": sent,
            "onset": onset,
            "ends": ends,
            "expires": expires,
            "areaDesc": "Cleveland, OK",
            "geocode": {"UGC": list(ugc), "SAME": ["040027"]},
            "parameters": params,
        },
    }


# Polygon around the default point (35.22, -97.44); coordinates are lon/lat
HIT_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-97.55, 35.12], [-97.33, 35.12], [-97.33, 35.32],
                     [-97.55, 35.32], [-97.55, 35.12]]],
}
# Same-size polygon shifted north of the point: tagged with our county UGC but
# its polygon does not cover us, so geometry must win over UGC containment
MISS_POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-97.55, 35.42], [-97.33, 35.42], [-97.33, 35.62],
                     [-97.55, 35.62], [-97.55, 35.42]]],
}
