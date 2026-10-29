import datetime as dt
import unittest
from unittest.mock import patch

import mysql.connector.pooling


class _PoolWithoutDatabase:
    def __init__(self, *args, **kwargs):
        pass


with patch.object(mysql.connector.pooling, "MySQLConnectionPool", _PoolWithoutDatabase):
    import main


class _Connection:
    def __init__(self):
        self.in_transaction = False
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.in_transaction = False
        self.commits += 1

    def rollback(self):
        self.in_transaction = False
        self.rollbacks += 1


class SessionInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.booking = {
            "id": "booking-123",
            "status": "charging",
            "station_id": "station-123",
            "consumer_id": "consumer-123",
            "host_id": "host-123",
            "station_title": "Community Charger",
            "started_at": dt.datetime.utcnow() - dt.timedelta(hours=1),
            "pricing_type_at_start": "per_kwh",
            "base_rate_at_start": 12.5,
            "pricing_multiplier_at_start": 1.2,
            "pricing_rule_at_start": "Peak",
        }
        self.conn = _Connection()
        self.statements = []
        self.patchers = [
            patch.object(main, "begin", self._begin),
            patch.object(main, "q", self._query),
            patch.object(main, "exec_sql", self._execute),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _begin(self, conn):
        conn.in_transaction = True

    def _query(self, conn, sql, args=(), one=False):
        if "FROM bookings b JOIN charging_stations" in sql or "FROM bookings WHERE id=" in sql:
            return dict(self.booking)
        raise AssertionError(f"Unexpected query: {sql}")

    def _execute(self, conn, sql, args=()):
        self.statements.append((sql, args))
        if "UPDATE bookings SET status='completed'" in sql:
            self.booking["status"] = "completed"
            self.booking["ended_at"] = args[0]
            self.booking["energy_consumed"] = args[1]
        return 1

    def _interrupt(self, user_id="host-123", reason="Power cut"):
        return main.interrupt_session(
            "booking-123",
            main.SessionInterruptionIn(reason=reason, description="Grid outage"),
            conn=self.conn,
            user={"id": user_id, "role": "host"},
        )

    def test_interruption_uses_start_snapshot_and_creates_one_bill_and_two_notifications(self):
        result = self._interrupt()

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["estimated"])
        self.assertAlmostEqual(result["energy_kwh"], 7.2, places=1)
        self.assertAlmostEqual(result["total_amount"], 108.0, places=1)
        self.assertEqual(self.booking["status"], "completed")
        self.assertEqual(
            sum("INSERT INTO transactions" in sql for sql, _ in self.statements),
            1,
        )
        self.assertEqual(
            sum("INSERT INTO session_interruptions" in sql for sql, _ in self.statements),
            1,
        )
        notification_inserts = [
            args for sql, args in self.statements if "INSERT INTO user_notifications" in sql
        ]
        self.assertEqual(len(notification_inserts), 2)
        self.assertEqual({args[1] for args in notification_inserts}, {"consumer-123", "host-123"})
        self.assertTrue(any("estimated" in args[3].lower() for args in notification_inserts))
        self.assertEqual(self.conn.commits, 1)

    def test_completed_session_cannot_be_interrupted_or_billed_twice(self):
        self._interrupt()
        statement_count = len(self.statements)

        with self.assertRaises(main.HTTPException) as caught:
            self._interrupt()

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.statements), statement_count)

    def test_only_the_station_host_can_report_an_interruption(self):
        with self.assertRaises(main.HTTPException) as caught:
            self._interrupt(user_id="another-host")

        self.assertEqual(caught.exception.status_code, 404)
        self.assertFalse(self.statements)
        self.assertEqual(self.conn.rollbacks, 1)

    def test_interruption_reason_is_restricted(self):
        with self.assertRaises(main.HTTPException) as caught:
            self._interrupt(reason="Automatic power cut detection")

        self.assertEqual(caught.exception.status_code, 422)
        self.assertFalse(self.statements)

    def test_regular_consumer_stop_uses_the_snapshot_and_remains_separate(self):
        result = main.stop(
            "booking-123",
            main.StopIn(energy_kwh=10),
            conn=self.conn,
            user={"id": "consumer-123", "role": "consumer"},
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_amount"], 150.0)
        self.assertFalse(any("INSERT INTO session_interruptions" in sql for sql, _ in self.statements))
        self.assertEqual(
            sum("INSERT INTO transactions" in sql for sql, _ in self.statements),
            1,
        )


if __name__ == "__main__":
    unittest.main()
