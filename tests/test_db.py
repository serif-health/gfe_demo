from gfe_service.db import connect, init_db
from gfe_service.db import seed_data as s


def test_init_adds_missing_reference_data_without_changing_prices(settings):
    conn = connect(settings.db_path)
    try:
        conn.execute("DELETE FROM plans WHERE payer_id = 'aetna'")
        conn.execute("DELETE FROM payers WHERE payer_id = 'aetna'")
        conn.execute("DELETE FROM providers WHERE npi = ?", (s.HOFFMAN,))
        conn.execute("UPDATE providers SET name = 'Custom hospital' WHERE npi = ?", (s.EMERSON,))
        conn.execute("UPDATE negotiated_rates SET negotiated_rate = 123")
        rates = [tuple(row) for row in conn.execute("SELECT * FROM negotiated_rates ORDER BY id")]
        charges = [tuple(row) for row in conn.execute("SELECT * FROM standard_charges ORDER BY npi, code")]
        conn.commit()

        init_db(settings.db_path)
        init_db(settings.db_path)

        assert conn.execute("SELECT name FROM payers WHERE payer_id = 'aetna'").fetchone()[0] == "Aetna"
        assert conn.execute("SELECT npi FROM providers WHERE npi = ?", (s.HOFFMAN,)).fetchone()[0] == s.HOFFMAN
        assert conn.execute("SELECT price_network_id FROM plans WHERE plan_id = 'aetna-oamc'").fetchone()[0] == s.AETNA_OAMC
        assert conn.execute("SELECT name FROM providers WHERE npi = ?", (s.EMERSON,)).fetchone()[0] == "Custom hospital"
        assert [tuple(row) for row in conn.execute("SELECT * FROM negotiated_rates ORDER BY id")] == rates
        assert [tuple(row) for row in conn.execute("SELECT * FROM standard_charges ORDER BY npi, code")] == charges
    finally:
        conn.close()
