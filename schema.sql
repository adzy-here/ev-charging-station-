CREATE DATABASE IF NOT EXISTS ev_charging
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE ev_charging;

CREATE TABLE IF NOT EXISTS users (
    id CHAR(36) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('consumer', 'host') NOT NULL DEFAULT 'consumer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS charging_stations (
    id CHAR(36) PRIMARY KEY,
    host_id CHAR(36) NOT NULL,
    title VARCHAR(100) NOT NULL,
    connector_type VARCHAR(50) NOT NULL,

    -- MySQL 8.x spatial column using WGS84.
    coordinates POINT SRID 4326 NOT NULL,

    pricing_type ENUM('per_kwh', 'per_hour') NOT NULL,
    price_rate DECIMAL(10,2) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (host_id) REFERENCES users(id) ON DELETE CASCADE,
    SPATIAL INDEX idx_stations_coordinates (coordinates)
);

CREATE TABLE IF NOT EXISTS time_slots (
    id CHAR(36) PRIMARY KEY,
    station_id CHAR(36) NOT NULL,
    start_time DATETIME NOT NULL,
    end_time DATETIME NOT NULL,
    is_booked BOOLEAN NOT NULL DEFAULT FALSE,

    FOREIGN KEY (station_id) REFERENCES charging_stations(id) ON DELETE CASCADE,
    CONSTRAINT chk_time_order CHECK (start_time < end_time),
    INDEX idx_slots_station_time (station_id, start_time)
);

CREATE TABLE IF NOT EXISTS bookings (
    id CHAR(36) PRIMARY KEY,
    consumer_id CHAR(36) NOT NULL,
    station_id CHAR(36) NOT NULL,
    slot_id CHAR(36) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'reserved',
    started_at DATETIME NULL,
    ended_at DATETIME NULL,
    energy_consumed DECIMAL(6,2) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (consumer_id) REFERENCES users(id),
    FOREIGN KEY (station_id) REFERENCES charging_stations(id),
    FOREIGN KEY (slot_id) REFERENCES time_slots(id),

    INDEX idx_bookings_consumer (consumer_id),
    INDEX idx_bookings_station (station_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id CHAR(36) PRIMARY KEY,
    booking_id CHAR(36) NOT NULL UNIQUE,
    total_amount DECIMAL(10,2) NOT NULL,
    platform_fee DECIMAL(10,2) NOT NULL,
    host_payout DECIMAL(10,2) NOT NULL,
    status ENUM('pending', 'completed', 'failed') NOT NULL DEFAULT 'pending',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE RESTRICT
);
