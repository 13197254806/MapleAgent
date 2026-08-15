-- MapleAgent V1 persistence schema for MySQL 8.0+ / 9.x.
-- Replay-heavy data (frames, perception, WorldState, decisions and metrics)
-- intentionally remains in the per-session recording directory.

CREATE DATABASE IF NOT EXISTS maple_agent
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

USE maple_agent;

CREATE TABLE IF NOT EXISTS sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id VARCHAR(64) NOT NULL,
    started_at_ms BIGINT UNSIGNED NOT NULL,
    ended_at_ms BIGINT UNSIGNED NULL,
    status VARCHAR(32) NOT NULL,
    client_version VARCHAR(64) NULL,
    recording_path VARCHAR(1024) NOT NULL,
    config_json JSON NOT NULL,
    last_frame_id BIGINT NOT NULL DEFAULT -1,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_sessions_session_id (session_id),
    KEY ix_sessions_started_at_ms (started_at_ms),
    KEY ix_sessions_status (status)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS session_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id VARCHAR(64) NOT NULL,
    occurred_at_ms BIGINT UNSIGNED NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    details_json JSON NOT NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY ix_session_events_session_time (session_id, occurred_at_ms),
    KEY ix_session_events_type_time (event_type, occurred_at_ms),
    CONSTRAINT fk_session_events_session
        FOREIGN KEY (session_id) REFERENCES sessions (session_id)
        ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS action_plans (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id VARCHAR(64) NOT NULL,
    plan_id BIGINT UNSIGNED NOT NULL,
    based_on_frame_id BIGINT UNSIGNED NOT NULL,
    created_at_ms BIGINT UNSIGNED NOT NULL,
    ttl_ms INT UNSIGNED NOT NULL,
    fsm_state VARCHAR(32) NOT NULL,
    intent_type VARCHAR(32) NOT NULL,
    expected_result VARCHAR(512) NOT NULL,
    plan_json JSON NOT NULL,
    ack_status VARCHAR(32) NULL,
    ack_detail VARCHAR(512) NULL,
    acked_at_ms BIGINT UNSIGNED NULL,
    created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uq_action_plans_session_plan (session_id, plan_id),
    KEY ix_action_plans_created_at_ms (created_at_ms),
    KEY ix_action_plans_ack_status (ack_status),
    CONSTRAINT fk_action_plans_session
        FOREIGN KEY (session_id) REFERENCES sessions (session_id)
        ON DELETE CASCADE
) ENGINE=InnoDB;
