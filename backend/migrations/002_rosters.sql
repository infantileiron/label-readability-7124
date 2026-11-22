CREATE TABLE rosters (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    method text NOT NULL,
    checksum text NOT NULL UNIQUE,
    previous_id bigint REFERENCES rosters(id),
    result jsonb NOT NULL,
    requested_by text NOT NULL REFERENCES accounts(username),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE roster_items (
    roster_id bigint NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    position int NOT NULL,
    study_id bigint NOT NULL REFERENCES studies(id),
    run_id bigint NOT NULL REFERENCES analysis_runs(id),
    PRIMARY KEY (roster_id, position),
    UNIQUE (roster_id, study_id),
    UNIQUE (roster_id, run_id)
);

CREATE TABLE roster_members (
    roster_id bigint NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    visitor_code text NOT NULL,
    study_id bigint NOT NULL REFERENCES studies(id),
    run_id bigint NOT NULL REFERENCES analysis_runs(id),
    reading_order text NOT NULL CHECK (reading_order IN ('old_first', 'new_first')),
    PRIMARY KEY (roster_id, visitor_code),
    FOREIGN KEY (roster_id, study_id) REFERENCES roster_items(roster_id, study_id)
);
CREATE INDEX roster_members_order ON roster_members(roster_id, study_id, reading_order);
