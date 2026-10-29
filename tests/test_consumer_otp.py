import datetime as dt
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import mysql.connector.pooling


class _PoolWithoutDatabase:
    def __init__(self, *args, **kwargs):
        pass


with patch.object(mysql.connector.pooling, "MySQLConnectionPool", _PoolWithoutDatabase):
    import main


class _Connection:
    def __init__(self):
        self.lock = threading.RLock()
        self.in_transaction = False

    def commit(self):
        if self.in_transaction:
            self.in_transaction = False
            self.lock.release()

    def rollback(self):
        self.commit()


class ConsumerOtpTests(unittest.TestCase):
    def setUp(self):
        now = dt.datetime.utcnow()
        self.booking_id = "booking-123"
        self.code = "012345"
        self.booking = {
            "id": self.booking_id,
            "status": "reserved",
            "station_id": "station-123",
            "slot_id": "slot-123",
            "consumer_id": "consumer-123",
            "host_id": "host-123",
            "pricing_type": "per_kwh",
            "price_rate": 12.5,
            "start_time": now - dt.timedelta(minutes=5),
            "end_time": now + dt.timedelta(minutes=30),
        }
        self.verification = {
            "otp_used_at": None,
            "otp_verified_at": None,
            "otp_expires_at": now + dt.timedelta(minutes=5),
            "otp_attempts": 0,
            "otp_last_sent_at": now,
            "otp_window_started_at": now,
            "otp_send_count": 1,
            "otp_hash": main.hmac.new(
                main.OTP_HASH_SECRET,
                f"{self.booking_id}:{self.code}".encode(),
                main.hashlib.sha256,
            ).hexdigest(),
        }
        self.conn = _Connection()
        self.patches = [
            patch.object(main, "begin", self._begin),
            patch.object(main, "q", self._query),
            patch.object(main, "exec_sql", self._execute),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _begin(self, conn):
        conn.lock.acquire()
        conn.in_transaction = True

    def _query(self, conn, sql, args=(), one=False):
        if "FROM bookings b JOIN charging_stations" in sql:
            return dict(self.booking)
        if "FROM station_pricing_rules" in sql:
            return []
        if "SELECT end_time FROM time_slots" in sql:
            return {"end_time": dt.datetime.utcnow() + dt.timedelta(minutes=30)}
        if "FROM booking_consumer_verifications" in sql:
            return dict(self.verification)
        raise AssertionError(f"Unexpected query: {sql}")

    def _execute(self, conn, sql, args=()):
        if "SET otp_attempts=%s" in sql:
            self.verification["otp_attempts"] = args[0]
        elif "SET otp_used_at=%s,otp_verified_at=%s" in sql:
            self.verification["otp_used_at"] = args[0]
            self.verification["otp_verified_at"] = args[1]
        elif "SET otp_hash=%s" in sql:
            self.verification.update({
                "otp_hash": args[0],
                "otp_expires_at": args[1],
                "otp_used_at": None,
                "otp_verified_at": None,
                "otp_attempts": 0,
                "otp_last_sent_at": args[2],
                "otp_window_started_at": args[3],
                "otp_send_count": args[4],
            })
        elif "SET otp_delivery_status='expired'" in sql:
            self.verification["otp_delivery_status"] = "expired"
        elif "UPDATE bookings SET status='charging'" in sql:
            self.booking["status"] = "charging"
            self.booking["pricing_type_at_start"] = args[1]
            self.booking["base_rate_at_start"] = args[2]
            self.booking["pricing_multiplier_at_start"] = args[3]
            self.booking["pricing_rule_at_start"] = args[4]
        else:
            raise AssertionError(f"Unexpected update: {sql}")
        return 1

    def _verify(self, code=None, user_id="host-123"):
        data = main.VerifyBookingOtpIn(otp_code=code or self.code)
        return main.verify_booking_otp(self.booking_id, data, conn=self.conn, user={"id": user_id, "role": "host"})

    def _start(self, user_id="host-123", role="host"):
        return main.start(self.booking_id, conn=self.conn, user={"id": user_id, "role": role})

    def test_phone_and_vehicle_are_normalized_and_validated(self):
        self.assertEqual(main.normalize_phone("+91 98765 43210"), "+919876543210")
        self.assertEqual(main.normalize_phone("09876543210"), "+919876543210")
        self.assertEqual(main.normalize_vehicle_registration("ka 01-ab 1234"), "KA01AB1234")
        with self.assertRaises(main.HTTPException):
            main.normalize_phone("12345")
        with self.assertRaises(main.HTTPException):
            main.normalize_vehicle_registration("NOT A PLATE")

    def test_slot_times_are_interpreted_in_ist_and_stored_as_utc(self):
        ist_wall_time = dt.datetime(2026, 10, 22, 9, 0)
        ist_aware_time = dt.datetime.fromisoformat("2026-10-22T09:00:00+05:30")
        expected_utc = dt.datetime(2026, 10, 22, 3, 30)
        self.assertEqual(main.slot_datetime_to_utc(ist_wall_time), expected_utc)
        self.assertEqual(main.slot_datetime_to_utc(ist_aware_time), expected_utc)

    def test_otp_hash_is_bound_to_booking_and_not_plaintext(self):
        code, digest = main.new_otp("booking-123")
        self.assertRegex(code, r"^\d{6}$")
        self.assertNotEqual(code, digest)
        self.assertTrue(main.otp_matches("booking-123", code, digest))
        self.assertFalse(main.otp_matches("another-booking", code, digest))

    def test_demo_delivery_status_does_not_call_an_sms_provider(self):
        self.assertEqual(main.demo_otp_delivery_status(), "development")

    def test_resend_cooldown_remaining_uses_full_180_seconds(self):
        now = dt.datetime.utcnow()
        self.assertEqual(main.otp_resend_seconds(now - dt.timedelta(seconds=179), now), 1)
        self.assertEqual(main.otp_resend_seconds(now - dt.timedelta(seconds=180), now), 0)

    def test_resend_endpoint_rejects_at_179_seconds_and_succeeds_at_180(self):
        booking = dict(self.booking, slot_id="slot-123")
        user = {"id": "consumer-123", "role": "consumer"}
        with patch.object(main, "owned_booking", return_value=booking), \
             patch.object(main, "reserve_otp_send"), \
             patch.object(main, "new_otp", return_value=("654321", "new-hash")), \
             patch.object(main, "save_otp_delivery_status"):
            self.verification["otp_last_sent_at"] = dt.datetime.utcnow() - dt.timedelta(seconds=179)
            with self.assertRaises(main.HTTPException) as caught:
                main.resend_booking_otp(self.booking_id, conn=self.conn, user=user)
            self.assertEqual(caught.exception.status_code, 429)

            self.verification["otp_last_sent_at"] = dt.datetime.utcnow() - dt.timedelta(seconds=180)
            result = main.resend_booking_otp(self.booking_id, conn=self.conn, user=user)
        self.assertEqual(result["otp_resend_after_seconds"], 180)
        self.assertEqual(result["status"], "reserved")
        self.assertEqual(result["session_status"], "otp_pending")
        self.assertEqual(result["demo_otp"], "654321")

    def test_resend_is_rejected_after_verification_or_terminal_status(self):
        user = {"id": "consumer-123", "role": "consumer"}
        with patch.object(main, "owned_booking", side_effect=lambda _conn, _bid, _user, _lock: dict(self.booking)), \
             patch.object(main, "reserve_otp_send"), \
             patch.object(main, "new_otp", return_value=("654321", "new-hash")):
            self.verification["otp_used_at"] = dt.datetime.utcnow()
            with self.assertRaises(main.HTTPException) as caught:
                main.resend_booking_otp(self.booking_id, conn=self.conn, user=user)
            self.assertEqual(caught.exception.status_code, 409)

            self.verification["otp_used_at"] = None
            for status in ("charging", "completed", "cancelled"):
                self.booking["status"] = status
                with self.assertRaises(main.HTTPException) as caught:
                    main.resend_booking_otp(self.booking_id, conn=self.conn, user=user)
                self.assertEqual(caught.exception.status_code, 409)
            self.booking["status"] = "reserved"

    def test_consumer_hourly_send_limit_is_enforced(self):
        now = dt.datetime.utcnow()
        limit_state = {"window_started_at": now, "send_count": 9}
        with patch.object(main, "exec_sql") as execute, patch.object(main, "q", return_value=limit_state):
            main.reserve_otp_send(None, "consumer-123", now)
        self.assertEqual(execute.call_args.args[2][1], 10)
        limit_state["send_count"] = 10
        with patch.object(main, "exec_sql"), patch.object(main, "q", return_value=limit_state):
            with self.assertRaises(main.HTTPException) as caught:
                main.reserve_otp_send(None, "consumer-123", now)
        self.assertEqual(caught.exception.status_code, 429)

    def test_host_verifies_then_starts_session_once(self):
        verified = self._verify()
        self.assertEqual(verified["status"], "reserved")
        self.assertEqual(verified["session_status"], "awaiting_host_start")
        self.assertEqual(self.booking["status"], "reserved")
        self.assertIsNotNone(self.verification["otp_verified_at"])
        result = self._start()
        self.assertEqual(result["status"], "charging")
        self.assertEqual(result["session_status"], "session_active")
        self.assertEqual(self.booking["status"], "charging")
        self.assertEqual(self.booking["pricing_type_at_start"], "per_kwh")
        self.assertEqual(self.booking["base_rate_at_start"], 12.5)
        self.assertEqual(self.booking["pricing_multiplier_at_start"], 1.0)
        self.assertIsNotNone(self.verification["otp_used_at"])
        with self.assertRaises(main.HTTPException) as caught:
            self._start()
        self.assertEqual(caught.exception.status_code, 409)

    def test_consumer_can_start_only_their_own_booking_after_verification(self):
        self._verify()
        result = self._start(user_id="consumer-123", role="consumer")
        self.assertEqual(result["status"], "charging")
        self.assertEqual(result["session_status"], "session_active")
        self.assertEqual(self.booking["status"], "charging")

    def test_consumer_cannot_start_unverified_or_another_consumers_booking(self):
        with self.assertRaises(main.HTTPException) as caught:
            self._start(user_id="consumer-123", role="consumer")
        self.assertEqual(caught.exception.status_code, 409)
        self._verify()
        with self.assertRaises(main.HTTPException) as caught:
            self._start(user_id="other-consumer", role="consumer")
        self.assertEqual(caught.exception.status_code, 404)

    def test_session_cannot_start_before_host_otp_verification(self):
        with self.assertRaises(main.HTTPException) as caught:
            self._start()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.booking["status"], "reserved")

    def test_only_assigned_host_can_start_after_verification(self):
        self._verify()
        with self.assertRaises(main.HTTPException) as caught:
            self._start(user_id="other-host")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.booking["status"], "reserved")

    def test_concurrent_start_requests_create_only_one_session(self):
        self._verify()
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            try:
                return self._start()
            except main.HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(result == 409 for result in results), 1)

    def test_wrong_code_attempts_are_limited(self):
        for _ in range(4):
            with self.assertRaises(main.HTTPException) as caught:
                self._verify("999999")
            self.assertEqual(caught.exception.status_code, 400)
        with self.assertRaises(main.HTTPException) as caught:
            self._verify("999999")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(self.verification["otp_attempts"], 5)

    def test_expired_code_and_unassigned_host_are_rejected(self):
        self.verification["otp_expires_at"] = dt.datetime.utcnow() - dt.timedelta(seconds=1)
        with self.assertRaises(main.HTTPException) as caught:
            self._verify()
        self.assertEqual(caught.exception.status_code, 410)
        self.verification["otp_expires_at"] = dt.datetime.utcnow() + dt.timedelta(minutes=5)
        with self.assertRaises(main.HTTPException) as caught:
            self._verify(user_id="other-host")
        self.assertEqual(caught.exception.status_code, 404)

    def test_consumer_and_host_role_guards_are_distinct(self):
        with self.assertRaises(main.HTTPException) as caught:
            main.role("consumer")(user={"role": "host"})
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(main.role("host")(user={"role": "host"})["role"], "host")
        with self.assertRaises(main.HTTPException) as caught:
            main.role("host")(user={"role": "consumer"})
        self.assertEqual(caught.exception.status_code, 403)

    def test_only_host_role_can_verify_otp(self):
        route = next(route for route in main.app.routes if getattr(route, "path", None) == "/bookings/{booking_id}/otp/verify")
        host_guard = next(dependency for dependency in route.dependant.dependencies if dependency.call.__name__ == "check")
        with self.assertRaises(main.HTTPException) as caught:
            host_guard.call(user={"id": "consumer-123", "role": "consumer"})
        self.assertEqual(caught.exception.status_code, 403)

    def test_verified_code_cannot_be_reused(self):
        self._verify()
        with self.assertRaises(main.HTTPException) as caught:
            self._verify()
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
