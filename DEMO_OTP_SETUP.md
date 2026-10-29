
# Hackathon demo OTP

This implementation is demo-only. It does not send SMS, call an SMS provider,
or require SMS billing credentials.

For an existing installation, apply `migrations/007_host_session_start.sql`
after migration 006, then restart FastAPI. Fresh databases created from
`schema.sql` already include the OTP verification timestamp.

From PowerShell, open MySQL from the project directory so the migration path
does not include the Japanese characters in the workspace path:

```powershell
Set-Location -LiteralPath 'C:\Users\ADVITH\OneDrive\ドキュメント\voltway'
mysql --login-path=client --database=ev_charging
```

At the `mysql>` prompt, run `SOURCE migrations/007_host_session_start.sql;`.
Use your configured MySQL login-path name if it is not `client`.

After a consumer confirms a booking, the backend generates a random
booking-specific six-digit OTP, stores only its keyed hash, and returns the
code to the consumer. The confirmation displays **DEMO OTP — no SMS sent**.
The assigned host verifies the code with
`POST /bookings/{booking_id}/otp/verify`. OTP verification is recorded without
starting the session. The consumer then sees “Successfully verified” and can
start their own verified booking with `POST /bookings/{booking_id}/start`.
The assigned host can also start that booking from the host dashboard. Both
operations require authentication and lock the booking during state changes;
consumers cannot verify OTPs, start another consumer's booking, or start before
verification. The consumer and host dashboards reload persisted status from
the backend.

Codes expire after 10 minutes, allow five host attempts, and can be used once.
Consumers can generate a replacement after a strict 180-second cooldown, limited to
four codes per booking per hour and ten per consumer per hour.
The resend endpoint enforces the cooldown from `otp_last_sent_at`; the consumer
booking view returns the remaining delay and persisted booking/session status.
After verification, the OTP is marked used and the booking awaits a start
action. After the session starts, the booking changes to `charging`, so neither
role is offered an OTP resend flow.

`OTP_HASH_SECRET` may be set to a random server-side secret of at least 32
characters. If unset, the application uses `JWT_SECRET` for OTP hashing.
There are no SMS provider settings or credentials to configure. Existing
reserved bookings made before applying the migration lack verification details
and must be cancelled and recreated.

Host availability slots are entered and displayed in Indian Standard Time
(IST, UTC+05:30). The API converts these instants to UTC before storing them;
slot times returned by the API are rendered in IST in the browser.
