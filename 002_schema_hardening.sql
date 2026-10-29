-- 002_schema_hardening.sql  (MySQL 8.0.16+)
-- Upgrades an EXISTING ev_charging database to the hardened schema.sql.
-- BACK UP FIRST. Run each pre-flight query; every one should return zero rows.
-- Not covered here: the CHAR(36) -> ascii charset change for id columns (fresh installs only;
-- converting means dropping and re-adding every FK). Everything else is applied.

USE ev_charging;

-- ---------------- Pre-flight checks (all must return 0 rows) ----------------
SELECT DISTINCT status FROM charging_stations
 WHERE status NOT IN ('active','inactive','maintenance','suspended');
SELECT DISTINCT status FROM bookings
 WHERE status NOT IN ('reserved','charging','completed','cancelled','no_show');
SELECT b.id FROM bookings b JOIN time_slots s ON s.id = b.slot_id
 WHERE s.station_id <> b.station_id;
SELECT slot_id, COUNT(*) c FROM bookings
 WHERE status IN ('reserved','charging','completed') GROUP BY slot_id HAVING c > 1;
SELECT id FROM bookings WHERE ended_at IS NOT NULL AND (started_at IS NULL OR ended_at < started_at);
SELECT id FROM transactions WHERE total_amount <> platform_fee + host_payout;
SELECT id FROM station_pricing_rules WHERE start_time = end_time;
SELECT a.id, b.id FROM time_slots a JOIN time_slots b
  ON a.station_id = b.station_id AND a.id < b.id
 AND a.start_time < b.end_time AND a.end_time > b.start_time;
SELECT a.id, b.id FROM station_pricing_rules a JOIN station_pricing_rules b
  ON a.station_id = b.station_id AND a.id < b.id AND a.active AND b.active
 AND (a.start_time < a.end_time AND b.start_time < b.end_time AND a.start_time < b.end_time AND a.end_time > b.start_time);
-- (wrap-around pricing overlaps are caught by the triggers going forward)

-- ---------------- Schema changes ----------------

ALTER TABLE users
  ADD COLUMN deleted_at TIMESTAMP NULL DEFAULT NULL;

ALTER TABLE charging_stations
  ADD COLUMN currency CHAR(3) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'INR',
  ADD COLUMN deleted_at TIMESTAMP NULL DEFAULT NULL,
  MODIFY COLUMN status ENUM('active','inactive','maintenance','suspended') NOT NULL DEFAULT 'active',
  ADD CONSTRAINT chk_station_currency CHECK (currency REGEXP '^[A-Z]{3}$');

ALTER TABLE charging_stations DROP FOREIGN KEY fk_station_host;
ALTER TABLE charging_stations
  ADD CONSTRAINT fk_station_host FOREIGN KEY (host_id) REFERENCES users(id) ON DELETE RESTRICT;

ALTER TABLE time_slots
  ADD UNIQUE KEY uq_slots_id_station (id, station_id);

ALTER TABLE bookings
  MODIFY COLUMN status ENUM('reserved','charging','completed','cancelled','no_show') NOT NULL DEFAULT 'reserved',
  ADD COLUMN held_slot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_general_ci
      GENERATED ALWAYS AS (CASE WHEN status IN ('reserved','charging','completed') THEN slot_id ELSE NULL END) VIRTUAL,
  ADD UNIQUE KEY uq_bookings_held_slot (held_slot_id),
  ADD INDEX idx_bookings_slot_station (slot_id, station_id),
  ADD CONSTRAINT chk_booking_times CHECK (ended_at IS NULL OR (started_at IS NOT NULL AND ended_at >= started_at));

ALTER TABLE bookings DROP FOREIGN KEY fk_booking_slot;
ALTER TABLE bookings
  ADD CONSTRAINT fk_booking_slot_station FOREIGN KEY (slot_id, station_id) REFERENCES time_slots(id, station_id);

ALTER TABLE transactions
  ADD COLUMN currency CHAR(3) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'INR',
  ADD CONSTRAINT chk_transaction_split CHECK (total_amount = platform_fee + host_payout),
  ADD CONSTRAINT chk_transaction_currency CHECK (currency REGEXP '^[A-Z]{3}$');

ALTER TABLE station_pricing_rules
  ADD CONSTRAINT chk_pricing_window CHECK (start_time <> end_time);

-- Resync is_booked with bookings once, since the triggers only act on future changes.
UPDATE time_slots s
   SET s.is_booked = EXISTS (SELECT 1 FROM bookings b
                              WHERE b.slot_id = s.id
                                AND b.status IN ('reserved','charging','completed'));

-- ---------------- Function and triggers ----------------
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
