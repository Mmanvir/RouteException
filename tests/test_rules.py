from app.services.rules import haversine_m, normalise_gate, run_rules


def rules_of(ex, cfg) -> set[str]:
    return {c.rule for c in run_rules(ex, cfg)}


def test_clean_delivery_has_no_contradictions(make_ex, rules_cfg):
    assert rules_of(make_ex(), rules_cfg) == set()


def test_haversine_known_distance():
    # 0.001 deg latitude ~ 111 m
    assert 110 < haversine_m(30.0, 76.0, 30.001, 76.0) < 112
    assert haversine_m(30.0, 76.0, 30.0, 76.0) == 0


def test_status_vs_customer_delivered_but_not_received(make_ex, rules_cfg):
    ex = make_ex(customer=[("not_received", "I never received it")])
    assert "status_vs_customer" in rules_of(ex, rules_cfg)


def test_status_vs_customer_attempted_but_home(make_ex, rules_cfg):
    ex = make_ex(final_status="attempted", note=True, customer=[("customer_was_home", "I was home")])
    assert "status_vs_customer" in rules_of(ex, rules_cfg)


def test_photo_gate_mismatch(make_ex, rules_cfg):
    assert "photo_vs_address" in rules_of(make_ex(gate_seen="241"), rules_cfg)


def test_photo_gate_normalisation_matches():
    assert normalise_gate("Gate No. 214") == normalise_gate("214") == "214"
    assert normalise_gate("G-14") == "14"


def test_photo_without_door_on_delivered(make_ex, rules_cfg):
    assert "photo_vs_address" in rules_of(make_ex(gate_seen=None, door=False), rules_cfg)


def test_status_vs_driver_note(make_ex, rules_cfg):
    assert "status_vs_driver_note" in rules_of(make_ex(driver=["customer_absent"]), rules_cfg)
    ex = make_ex(final_status="attempted", driver=["delivered"])
    assert "status_vs_driver_note" in rules_of(ex, rules_cfg)


def test_time_gap_between_scans(make_ex, rules_cfg):
    assert "time_gap" in rules_of(make_ex(gap_hours=30), rules_cfg)
    assert "time_gap" not in rules_of(make_ex(gap_hours=20), rules_cfg)


def test_time_gap_photo_vs_scan(make_ex, rules_cfg):
    assert "time_gap" in rules_of(make_ex(photo_delay_min=95), rules_cfg)


def test_missing_photo(make_ex, rules_cfg):
    assert "missing_evidence" in rules_of(make_ex(photo=False), rules_cfg)


def test_missing_gps(make_ex, rules_cfg):
    assert "missing_evidence" in rules_of(make_ex(gps=None), rules_cfg)


def test_missing_note_on_attempt(make_ex, rules_cfg):
    assert "missing_evidence" in rules_of(make_ex(final_status="attempted"), rules_cfg)


def test_gps_vs_address_threshold(make_ex, rules_cfg):
    assert "gps_vs_address" in rules_of(make_ex(gps=(30.7030, 76.7000)), rules_cfg)  # ~333 m
    assert "gps_vs_address" not in rules_of(make_ex(gps=(30.7010, 76.7000)), rules_cfg)  # ~111 m


def test_rules_are_deterministic(make_ex, rules_cfg):
    ex = make_ex(gate_seen="99", gps=(30.71, 76.70), customer=[("not_received", "x")])
    runs = [[c.model_dump() for c in run_rules(ex, rules_cfg)] for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_every_contradiction_cites_sources(make_ex, rules_cfg):
    ex = make_ex(gate_seen="99", gps=(30.71, 76.70), customer=[("not_received", "x")], photo_delay_min=90)
    for c in run_rules(ex, rules_cfg):
        assert c.evidence and all(s.file and s.locator for s in c.evidence)
