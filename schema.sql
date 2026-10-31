-- EV SmartCharge schema (MySQL 8.0.16+)
-- Fresh install:    mysql < schema.sql
-- Existing install: back up first, then run migrations/002_schema_hardening.sql,
--                   migrations/003_fix_trigger_locking.sql,
--                   migrations/004_consumer_strikes.sql, migrations/005_station_address.sql,
--                   migrations/006_consumer_otp.sql, migrations/007_host_session_start.sql,
--                   migrations/008_profile_features.sql, and migrations/009_session_interruptions.sql
--                   (CREATE TABLE IF NOT EXISTS never alters existing tables).
--
-- CONVENTIONS
--  * Time zone: every DATETIME column holds UTC. Set the connection time zone to
--    '+00:00' in the API (e.g. SET time_zone = '+00:00') so TIMESTAMP columns agree.
--  * Coordinates: POINT SRID 4326 uses (latitude, longitude) axis order by default
--    in MySQL 8. Use ST_SRID/ST_PointFromText consistently in the API.
--  * Money: DECIMAL amounts plus an ISO-4217 currency code (default 'INR').
--  * IDs: CHAR(36) UUIDs, stored as ascii (36 bytes per value instead of 144).
--  * Deletes: users and stations are soft-deleted via deleted_at; money records
--    (bookings, transactions) are never cascaded away.

CREATE DATABASE IF NOT EXISTS ev_charging
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE ev_charging;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    phone_number VARCHAR(16) NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('consumer', 'host') NOT NULL DEFAULT 'consumer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP NULL DEFAULT NULL,
    banned_at TIMESTAMP NULL DEFAULT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS user_vehicles (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    nickname VARCHAR(60) NOT NULL,
    make VARCHAR(60) NOT NULL,
    model VARCHAR(60) NOT NULL,
    registration VARCHAR(20) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_vehicle_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE KEY uq_user_vehicle_registration (user_id, registration)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS user_devices (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    name VARCHAR(80) NOT NULL,
    device_type VARCHAR(40) NOT NULL DEFAULT 'Mobile app',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_device_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_user_devices_user (user_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS login_sessions (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    user_agent VARCHAR(255) NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMP NULL DEFAULT NULL,
    CONSTRAINT fk_login_session_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_login_sessions_user (user_id, revoked_at, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS charging_stations (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    host_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    title VARCHAR(100) NOT NULL,
    address VARCHAR(250) NULL,
    connector_type VARCHAR(50) NOT NULL,
    coordinates POINT SRID 4326 NOT NULL,
    pricing_type ENUM('per_kwh', 'per_hour') NOT NULL,
    price_rate DECIMAL(10,2) NOT NULL,
    currency CHAR(3) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'INR',
    status ENUM('active', 'inactive', 'maintenance', 'suspended') NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deleted_at TIMESTAMP NULL DEFAULT NULL,
    CONSTRAINT fk_station_host FOREIGN KEY (host_id) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_station_price CHECK (price_rate > 0),
    CONSTRAINT chk_station_currency CHECK (currency REGEXP '^[A-Z]{3}$'),
    SPATIAL INDEX idx_stations_coordinates (coordinates),
    INDEX idx_stations_host_status (host_id, status)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS time_slots (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    station_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    start_time DATETIME NOT NULL,  -- UTC
    end_time DATETIME NOT NULL,    -- UTC
    -- Maintained by triggers on bookings; do not write directly.
    is_booked BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT fk_slot_station FOREIGN KEY (station_id) REFERENCES charging_stations(id) ON DELETE CASCADE,
    CONSTRAINT chk_time_order CHECK (start_time < end_time),
    -- Target for bookings' composite FK, guarantees booking.station_id matches the slot's station.
    UNIQUE KEY uq_slots_id_station (id, station_id),
    INDEX idx_slots_station_time (station_id, start_time, end_time),
    INDEX idx_slots_availability (station_id, is_booked, start_time)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS bookings (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    consumer_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    station_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    slot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    status ENUM('reserved', 'charging', 'completed', 'cancelled', 'no_show') NOT NULL DEFAULT 'reserved',
    started_at DATETIME NULL,  -- UTC
    pricing_type_at_start VARCHAR(20) NULL,
    base_rate_at_start DECIMAL(10,2) NULL,
    pricing_multiplier_at_start DECIMAL(5,2) NULL,
    pricing_rule_at_start VARCHAR(80) NULL,
    ended_at DATETIME NULL,    -- UTC
    energy_consumed DECIMAL(6,2) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- A slot is "held" unless the booking was cancelled or a no-show. The unique key
    -- below makes double-booking impossible at the database level.
    held_slot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci
        GENERATED ALWAYS AS (
            CASE WHEN status IN ('reserved', 'charging', 'completed') THEN slot_id ELSE NULL END
        ) VIRTUAL,
    CONSTRAINT fk_booking_consumer FOREIGN KEY (consumer_id) REFERENCES users(id),
    CONSTRAINT fk_booking_station FOREIGN KEY (station_id) REFERENCES charging_stations(id),
    CONSTRAINT fk_booking_slot_station FOREIGN KEY (slot_id, station_id) REFERENCES time_slots(id, station_id),
    CONSTRAINT chk_booking_energy CHECK (energy_consumed IS NULL OR energy_consumed >= 0),
    CONSTRAINT chk_booking_times CHECK (ended_at IS NULL OR (started_at IS NOT NULL AND ended_at >= started_at)),
    UNIQUE KEY uq_bookings_held_slot (held_slot_id),
    INDEX idx_bookings_consumer (consumer_id, created_at),
    INDEX idx_bookings_station (station_id, status),
    INDEX idx_bookings_slot_status (slot_id, status),
    INDEX idx_bookings_slot_station (slot_id, station_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS booking_consumer_verifications (
    booking_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    consumer_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    phone_number VARCHAR(16) NOT NULL,
    vehicle_registration VARCHAR(20) NOT NULL,
    otp_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    otp_expires_at DATETIME NOT NULL,
    otp_used_at DATETIME NULL,
    otp_verified_at DATETIME NULL,
    otp_attempts TINYINT UNSIGNED NOT NULL DEFAULT 0,
    otp_last_sent_at DATETIME NOT NULL,
    otp_window_started_at DATETIME NOT NULL,
    otp_send_count TINYINT UNSIGNED NOT NULL DEFAULT 1,
    otp_delivery_status ENUM('pending', 'development', 'sent', 'failed', 'expired', 'used') NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_booking_verification_booking FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT,
    CONSTRAINT fk_booking_verification_consumer FOREIGN KEY (consumer_id) REFERENCES users(id) ON DELETE RESTRICT,
    INDEX idx_booking_verification_consumer (consumer_id, created_at),
    INDEX idx_booking_verification_resend (consumer_id, otp_last_sent_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS consumer_otp_rate_limits (
    consumer_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    window_started_at DATETIME NOT NULL,
    send_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
    CONSTRAINT fk_consumer_otp_rate_limit_user FOREIGN KEY (consumer_id) REFERENCES users(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS consumer_strikes (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    consumer_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    host_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    booking_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    reason VARCHAR(500) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_strike_consumer FOREIGN KEY (consumer_id) REFERENCES users(id),
    CONSTRAINT fk_strike_host FOREIGN KEY (host_id) REFERENCES users(id),
    CONSTRAINT fk_strike_booking FOREIGN KEY (booking_id) REFERENCES bookings(id),
    UNIQUE KEY uq_strikes_booking (booking_id),
    INDEX idx_strikes_consumer_created (consumer_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS transactions (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    booking_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL UNIQUE,
    total_amount DECIMAL(10,2) NOT NULL,
    platform_fee DECIMAL(10,2) NOT NULL,
    host_payout DECIMAL(10,2) NOT NULL,
    currency CHAR(3) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'INR',
    status ENUM('pending', 'completed', 'failed') NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_transaction_booking FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT,
    CONSTRAINT chk_transaction_amounts CHECK (total_amount >= 0 AND platform_fee >= 0 AND host_payout >= 0),
    CONSTRAINT chk_transaction_split CHECK (total_amount = platform_fee + host_payout),
    CONSTRAINT chk_transaction_currency CHECK (currency REGEXP '^[A-Z]{3}$'),
    INDEX idx_transactions_status_updated (status, updated_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS session_interruptions (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    booking_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL UNIQUE,
    host_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    reason ENUM('Power cut', 'Charger fault', 'Other') NOT NULL,
    description VARCHAR(1000) NOT NULL DEFAULT '',
    estimated_energy_kwh DECIMAL(6,2) NOT NULL,
    estimated_amount DECIMAL(10,2) NOT NULL,
    estimated BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_session_interruption_booking FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT,
    CONSTRAINT fk_session_interruption_host FOREIGN KEY (host_id) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT chk_interruption_estimates CHECK (estimated_energy_kwh >= 0 AND estimated_amount >= 0),
    INDEX idx_session_interruptions_host_created (host_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS user_notifications (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    notification_type VARCHAR(40) NOT NULL,
    title VARCHAR(120) NOT NULL,
    message VARCHAR(1000) NOT NULL,
    booking_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_notification_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_user_notification_booking FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT,
    UNIQUE KEY uq_user_notification_booking_type (user_id, booking_id, notification_type),
    INDEX idx_user_notifications_user_created (user_id, created_at)
) ENGINE=InnoDB;

-- Dynamic pricing: multiplier applies to the station's base price_rate.
-- Example: multiplier 0.80 for off-peak or 1.25 for peak time.
-- Windows are half-open [start_time, end_time). If end_time < start_time the window
-- wraps past midnight (e.g. 22:00 -> 06:00). Active rules for a station may not overlap
-- (enforced by triggers below).
CREATE TABLE IF NOT EXISTS station_pricing_rules (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    station_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    label VARCHAR(80) NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    multiplier DECIMAL(5,2) NOT NULL DEFAULT 1.00,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_pricing_rule_station FOREIGN KEY (station_id) REFERENCES charging_stations(id) ON DELETE CASCADE,
    CONSTRAINT chk_pricing_multiplier CHECK (multiplier > 0 AND multiplier <= 5),
    CONSTRAINT chk_pricing_window CHECK (start_time <> end_time),
    INDEX idx_pricing_rules_station_active (station_id, active)
) ENGINE=InnoDB;

-- One community review per consumer per station, only accepted by API after a completed session.
CREATE TABLE IF NOT EXISTS station_reviews (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    station_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    consumer_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    rating TINYINT UNSIGNED NOT NULL,
    comment VARCHAR(1200) NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_review_station FOREIGN KEY (station_id) REFERENCES charging_stations(id) ON DELETE CASCADE,
    CONSTRAINT fk_review_consumer FOREIGN KEY (consumer_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT chk_review_rating CHECK (rating BETWEEN 1 AND 5),
    UNIQUE KEY uq_review_consumer_station (consumer_id, station_id),
    INDEX idx_reviews_station_created (station_id, created_at)
) ENGINE=InnoDB;

-- Reliability reports are visible through aggregate indicators; hosts can later be given
-- a moderation endpoint to close/resolve reports.
CREATE TABLE IF NOT EXISTS station_reports (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    station_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    reporter_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    category ENUM('not_working', 'access_issue', 'safety', 'incorrect_listing', 'other') NOT NULL DEFAULT 'not_working',
    description VARCHAR(1200) NOT NULL,
    status ENUM('open', 'investigating', 'resolved', 'dismissed') NOT NULL DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_report_station FOREIGN KEY (station_id) REFERENCES charging_stations(id) ON DELETE CASCADE,
    CONSTRAINT fk_report_user FOREIGN KEY (reporter_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_reports_station_status (station_id, status),
    INDEX idx_reports_created (created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS user_station_preferences (
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    station_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    preference ENUM('saved', 'restricted') NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, station_id, preference),
    CONSTRAINT fk_user_station_preference_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_user_station_preference_station FOREIGN KEY (station_id) REFERENCES charging_stations(id) ON DELETE CASCADE,
    INDEX idx_user_station_preference_list (user_id, preference, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS user_profile_settings (
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    privacy_mode ENUM('private', 'public') NOT NULL DEFAULT 'private',
    notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    miles_demo_active BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_profile_settings_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS demo_wallet_entries (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    description VARCHAR(120) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_demo_wallet_entry_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT chk_demo_wallet_entry_amount CHECK (amount > 0),
    INDEX idx_demo_wallet_entries_user (user_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS demo_profile_orders (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci PRIMARY KEY,
    user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    item_code ENUM('charging_accessory_sample', 'miles_demo') NOT NULL,
    item_name VARCHAR(100) NOT NULL,
    amount DECIMAL(10,2) NOT NULL DEFAULT 0,
    status ENUM('demo_only') NOT NULL DEFAULT 'demo_only',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_demo_profile_order_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_demo_profile_orders_user (user_id, created_at)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------------
-- Functions and triggers (run with the mysql client so DELIMITER is honoured)
-- ---------------------------------------------------------------------------

DELIMITER $$

-- True if time-of-day p_t falls inside the half-open window [p_start, p_end),
-- where p_end < p_start means the window wraps past midnight.
DROP FUNCTION IF EXISTS fn_time_in_window$$
CREATE FUNCTION fn_time_in_window(p_start TIME, p_end TIME, p_t TIME)
RETURNS TINYINT(1)
DETERMINISTIC NO SQL
BEGIN
    IF p_start < p_end THEN
        RETURN (p_t >= p_start AND p_t < p_end);
    END IF;
    RETURN (p_t >= p_start OR p_t < p_end);
END$$

-- Slots of one station must not overlap. The locking read takes gap locks so
-- concurrent inserts for the same time range block instead of racing.
DROP TRIGGER IF EXISTS trg_slots_no_overlap_ins$$
CREATE TRIGGER trg_slots_no_overlap_ins BEFORE INSERT ON time_slots
FOR EACH ROW
BEGIN
    DECLARE v_overlaps INT DEFAULT 0;
    SELECT COUNT(*) INTO v_overlaps
      FROM time_slots
     WHERE station_id = NEW.station_id
       AND start_time < NEW.end_time
       AND end_time   > NEW.start_time
       FOR UPDATE;
    IF v_overlaps > 0 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Time slot overlaps an existing slot for this station';
    END IF;
END$$

DROP TRIGGER IF EXISTS trg_slots_no_overlap_upd$$
CREATE TRIGGER trg_slots_no_overlap_upd BEFORE UPDATE ON time_slots
FOR EACH ROW
BEGIN
    DECLARE v_overlaps INT DEFAULT 0;
    IF NEW.station_id <> OLD.station_id
       OR NEW.start_time <> OLD.start_time
       OR NEW.end_time <> OLD.end_time THEN
        SELECT COUNT(*) INTO v_overlaps
          FROM time_slots
         WHERE station_id = NEW.station_id
           AND id <> NEW.id
           AND start_time < NEW.end_time
           AND end_time   > NEW.start_time
           FOR UPDATE;
        IF v_overlaps > 0 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Time slot overlaps an existing slot for this station';
        END IF;
    END IF;
END$$

-- Keep time_slots.is_booked in sync with bookings. The unique key on
-- bookings.held_slot_id guarantees at most one held booking per slot.
DROP TRIGGER IF EXISTS trg_bookings_sync_slot_ins$$
CREATE TRIGGER trg_bookings_sync_slot_ins AFTER INSERT ON bookings
FOR EACH ROW
BEGIN
    IF NEW.status IN ('reserved', 'charging', 'completed') THEN
        UPDATE time_slots SET is_booked = TRUE WHERE id = NEW.slot_id;
    END IF;
END$$

DROP TRIGGER IF EXISTS trg_bookings_sync_slot_upd$$
CREATE TRIGGER trg_bookings_sync_slot_upd AFTER UPDATE ON bookings
FOR EACH ROW
BEGIN
    IF OLD.status IN ('reserved', 'charging', 'completed')
       AND (NEW.status NOT IN ('reserved', 'charging', 'completed') OR OLD.slot_id <> NEW.slot_id) THEN
        UPDATE time_slots SET is_booked = FALSE WHERE id = OLD.slot_id;
    END IF;
    IF NEW.status IN ('reserved', 'charging', 'completed') THEN
        UPDATE time_slots SET is_booked = TRUE WHERE id = NEW.slot_id;
    END IF;
END$$

DROP TRIGGER IF EXISTS trg_bookings_sync_slot_del$$
CREATE TRIGGER trg_bookings_sync_slot_del AFTER DELETE ON bookings
FOR EACH ROW
BEGIN
    IF OLD.status IN ('reserved', 'charging', 'completed') THEN
        UPDATE time_slots SET is_booked = FALSE WHERE id = OLD.slot_id;
    END IF;
END$$

-- Active pricing rules of one station must not overlap (wrap-around aware).
-- Two windows overlap iff the start of one lies inside the other.
DROP TRIGGER IF EXISTS trg_pricing_no_overlap_ins$$
CREATE TRIGGER trg_pricing_no_overlap_ins BEFORE INSERT ON station_pricing_rules
FOR EACH ROW
BEGIN
    DECLARE v_overlaps INT DEFAULT 0;
    IF NEW.active THEN
        SELECT COUNT(*) INTO v_overlaps
          FROM station_pricing_rules
         WHERE station_id = NEW.station_id
           AND active
           AND (fn_time_in_window(start_time, end_time, NEW.start_time)
                OR fn_time_in_window(NEW.start_time, NEW.end_time, start_time))
           FOR UPDATE;
        IF v_overlaps > 0 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Pricing rule overlaps an active rule for this station';
        END IF;
    END IF;
END$$

DROP TRIGGER IF EXISTS trg_pricing_no_overlap_upd$$
CREATE TRIGGER trg_pricing_no_overlap_upd BEFORE UPDATE ON station_pricing_rules
FOR EACH ROW
BEGIN
    DECLARE v_overlaps INT DEFAULT 0;
    IF NEW.active THEN
        SELECT COUNT(*) INTO v_overlaps
          FROM station_pricing_rules
         WHERE station_id = NEW.station_id
           AND id <> NEW.id
           AND active
           AND (fn_time_in_window(start_time, end_time, NEW.start_time)
                OR fn_time_in_window(NEW.start_time, NEW.end_time, start_time))
           FOR UPDATE;
        IF v_overlaps > 0 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Pricing rule overlaps an active rule for this station';
        END IF;
    END IF;
END$$

DELIMITER ;
