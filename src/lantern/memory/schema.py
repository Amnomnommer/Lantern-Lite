"""Versioned SQLite schema for Lantern's durable memory."""

from __future__ import annotations

SCHEMA_VERSION = 8

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(kind, canonical_name)
        )
        """,
        """
        CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL,
            excerpt TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE episodes (
            id TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            retention_class TEXT NOT NULL CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE relationships (
            id TEXT PRIMARY KEY,
            subject_entity_id TEXT NOT NULL REFERENCES entities(id),
            relationship_type TEXT NOT NULL,
            object_entity_id TEXT NOT NULL REFERENCES entities(id),
            source_id TEXT REFERENCES sources(id),
            episode_id TEXT REFERENCES episodes(id),
            confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
            created_at TEXT NOT NULL,
            CHECK (subject_entity_id <> object_entity_id),
            CHECK (source_id IS NOT NULL OR episode_id IS NOT NULL)
        )
        """,
        """
        CREATE TABLE claims (
            id TEXT PRIMARY KEY,
            subject_entity_id TEXT NOT NULL REFERENCES entities(id),
            predicate TEXT NOT NULL,
            object_text TEXT NOT NULL,
            evidence_type TEXT NOT NULL CHECK (
                evidence_type IN (
                    'observed', 'reported_by_amelie', 'retrieved',
                    'remembered', 'inferred', 'predicted', 'imagined',
                    'unknown'
                )
            ),
            confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
            source_id TEXT REFERENCES sources(id),
            episode_id TEXT REFERENCES episodes(id),
            consequential INTEGER NOT NULL DEFAULT 1 CHECK (consequential IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'active' CHECK (
                status IN ('active', 'superseded', 'retracted')
            ),
            retention_class TEXT NOT NULL DEFAULT 'ordinary' CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            created_at TEXT NOT NULL,
            CHECK (
                consequential = 0
                OR source_id IS NOT NULL
                OR episode_id IS NOT NULL
                OR evidence_type = 'unknown'
            )
        )
        """,
        """
        CREATE TABLE claim_links (
            id TEXT PRIMARY KEY,
            from_claim_id TEXT NOT NULL REFERENCES claims(id),
            to_claim_id TEXT NOT NULL REFERENCES claims(id),
            relation TEXT NOT NULL CHECK (
                relation IN (
                    'supports', 'contradicts', 'derived_from',
                    'corrects', 'supersedes'
                )
            ),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (from_claim_id <> to_claim_id),
            UNIQUE(from_claim_id, to_claim_id, relation)
        )
        """,
        """
        CREATE TABLE claim_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL REFERENCES claims(id),
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            changed_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX claims_subject_predicate_idx
        ON claims(subject_entity_id, predicate, status)
        """,
        """
        CREATE INDEX claim_links_from_idx
        ON claim_links(from_claim_id, relation)
        """,
        """
        CREATE INDEX claim_links_to_idx
        ON claim_links(to_claim_id, relation)
        """,
    ),
    2: (
        """
        CREATE TABLE preferences (
            id TEXT PRIMARY KEY,
            holder_entity_id TEXT NOT NULL REFERENCES entities(id),
            topic TEXT NOT NULL,
            preferred_option TEXT NOT NULL,
            perspective TEXT NOT NULL CHECK (
                perspective IN (
                    'lantern_self', 'owner_report',
                    'owner_approval_prediction'
                )
            ),
            level TEXT NOT NULL CHECK (
                level IN ('inclination', 'provisional', 'established')
            ),
            confidence REAL NOT NULL CHECK (
                confidence >= 0.0 AND confidence <= 1.0
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(
                holder_entity_id, topic, preferred_option, perspective
            )
        )
        """,
        """
        CREATE TABLE preference_evidence (
            id TEXT PRIMARY KEY,
            preference_id TEXT NOT NULL REFERENCES preferences(id),
            episode_id TEXT NOT NULL REFERENCES episodes(id),
            independence_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            alternative_text TEXT,
            tradeoff_present INTEGER NOT NULL DEFAULT 0 CHECK (
                tradeoff_present IN (0, 1)
            ),
            created_at TEXT NOT NULL,
            UNIQUE(preference_id, independence_key)
        )
        """,
        """
        CREATE INDEX preferences_holder_topic_idx
        ON preferences(holder_entity_id, topic, perspective)
        """,
        """
        CREATE INDEX preference_evidence_preference_idx
        ON preference_evidence(preference_id, created_at)
        """,
    ),
    3: (
        """
        CREATE TABLE episode_facts (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id),
            observation_key TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            evidence_type TEXT NOT NULL CHECK (
                evidence_type IN (
                    'observed', 'reported_by_amelie', 'retrieved',
                    'remembered', 'inferred', 'predicted', 'imagined',
                    'unknown'
                )
            ),
            required INTEGER NOT NULL CHECK (required IN (0, 1)),
            sequence INTEGER NOT NULL,
            UNIQUE(episode_id, observation_key)
        )
        """,
        """
        CREATE TABLE consolidation_records (
            staging_session_id TEXT PRIMARY KEY,
            source_digest TEXT NOT NULL,
            summary_digest TEXT NOT NULL,
            episode_id TEXT NOT NULL UNIQUE REFERENCES episodes(id),
            fidelity_status TEXT NOT NULL CHECK (
                fidelity_status = 'passed'
            ),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX episode_facts_episode_sequence_idx
        ON episode_facts(episode_id, sequence)
        """,
    ),
    4: (
        """
        CREATE TABLE deletion_audit (
            id TEXT PRIMARY KEY,
            target_kind TEXT NOT NULL CHECK (
                target_kind IN (
                    'claim', 'episode', 'preference', 'source', 'entity'
                )
            ),
            retention_class TEXT NOT NULL CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            requested_by TEXT NOT NULL,
            reason_code TEXT NOT NULL CHECK (
                reason_code IN (
                    'owner_request', 'privacy', 'correction',
                    'expiry', 'obsolete'
                )
            ),
            protected_reconfirmed INTEGER NOT NULL CHECK (
                protected_reconfirmed IN (0, 1)
            ),
            process TEXT NOT NULL CHECK (
                process = 'MemoryStore.forget'
            ),
            completed_at TEXT NOT NULL,
            backup_purge_due_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX deletion_audit_completed_idx
        ON deletion_audit(completed_at)
        """,
    ),
    5: (
        """
        CREATE TABLE integrity_snapshots (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            state_digest TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (
                reason IN ('manual', 'pre_backup', 'post_restore')
            ),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX integrity_snapshots_created_idx
        ON integrity_snapshots(created_at)
        """,
    ),
    6: (
        "DROP INDEX deletion_audit_completed_idx",
        "ALTER TABLE deletion_audit RENAME TO deletion_audit_v4",
        """
        CREATE TABLE deletion_audit (
            id TEXT PRIMARY KEY,
            target_kind TEXT NOT NULL CHECK (
                target_kind IN (
                    'claim', 'episode', 'preference', 'source', 'entity',
                    'cognitive_schema'
                )
            ),
            retention_class TEXT NOT NULL CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            requested_by TEXT NOT NULL,
            reason_code TEXT NOT NULL CHECK (
                reason_code IN (
                    'owner_request', 'privacy', 'correction',
                    'expiry', 'obsolete'
                )
            ),
            protected_reconfirmed INTEGER NOT NULL CHECK (
                protected_reconfirmed IN (0, 1)
            ),
            process TEXT NOT NULL CHECK (
                process = 'MemoryStore.forget'
            ),
            completed_at TEXT NOT NULL,
            backup_purge_due_at TEXT NOT NULL
        )
        """,
        """
        INSERT INTO deletion_audit
        SELECT * FROM deletion_audit_v4
        """,
        "DROP TABLE deletion_audit_v4",
        """
        CREATE INDEX deletion_audit_completed_idx
        ON deletion_audit(completed_at)
        """,
        """
        CREATE TABLE cognitive_schemas (
            id TEXT PRIMARY KEY,
            family TEXT NOT NULL CHECK (
                family IN (
                    'concept', 'situation', 'roles_social', 'causal',
                    'procedure', 'creative', 'self', 'ethical'
                )
            ),
            scope TEXT NOT NULL,
            privacy_class TEXT NOT NULL CHECK (
                privacy_class IN ('private', 'owner_shared', 'public')
            ),
            retention_class TEXT NOT NULL CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            lifecycle_state TEXT NOT NULL CHECK (
                lifecycle_state IN (
                    'candidate', 'provisional', 'active', 'contested',
                    'superseded', 'retired'
                )
            ),
            current_version INTEGER NOT NULL CHECK (current_version >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE cognitive_schema_versions (
            id TEXT PRIMARY KEY,
            schema_id TEXT NOT NULL REFERENCES cognitive_schemas(id)
                ON DELETE CASCADE,
            version_number INTEGER NOT NULL CHECK (version_number >= 1),
            parent_version_id TEXT REFERENCES cognitive_schema_versions(id),
            operation TEXT NOT NULL CHECK (
                operation IN (
                    'creation', 'assimilation', 'accommodation',
                    'differentiation', 'integration', 'inhibition',
                    'annotation'
                )
            ),
            description TEXT NOT NULL,
            representation_json TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (
                confidence >= 0.0 AND confidence <= 1.0
            ),
            revision_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(schema_id, version_number)
        )
        """,
        """
        CREATE TABLE cognitive_schema_evidence (
            id TEXT PRIMARY KEY,
            schema_version_id TEXT NOT NULL
                REFERENCES cognitive_schema_versions(id) ON DELETE CASCADE,
            claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
            relation TEXT NOT NULL CHECK (
                relation IN (
                    'supports', 'counterexample', 'contradicts', 'exception'
                )
            ),
            rationale TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(schema_version_id, claim_id, relation)
        )
        """,
        """
        CREATE INDEX cognitive_schemas_family_state_idx
        ON cognitive_schemas(family, lifecycle_state)
        """,
        """
        CREATE INDEX cognitive_schema_versions_schema_idx
        ON cognitive_schema_versions(schema_id, version_number)
        """,
        """
        CREATE INDEX cognitive_schema_evidence_version_idx
        ON cognitive_schema_evidence(schema_version_id, relation)
        """,
        """
        CREATE INDEX cognitive_schema_evidence_claim_idx
        ON cognitive_schema_evidence(claim_id)
        """,
    ),
    7: (
        """
        CREATE TABLE hive_nodes (
            node_id TEXT PRIMARY KEY,
            identity_name TEXT NOT NULL UNIQUE,
            node_kind TEXT NOT NULL CHECK (
                node_kind IN ('cognitive', 'relay')
            ),
            lifecycle_state TEXT NOT NULL CHECK (
                lifecycle_state IN (
                    'active', 'dormant', 'quarantined', 'revoked'
                )
            ),
            authority_scope TEXT NOT NULL CHECK (
                authority_scope IN (
                    'owner_controlled_single_host', 'auxiliary',
                    'distributed_cognitive'
                )
            ),
            credential_state TEXT NOT NULL CHECK (
                credential_state IN ('pending', 'active', 'rotated', 'revoked')
            ),
            credential_fingerprint TEXT,
            authoritative INTEGER NOT NULL CHECK (authoritative IN (0, 1)),
            enrolled_at TEXT NOT NULL,
            CHECK (
                credential_state = 'pending'
                OR credential_fingerprint IS NOT NULL
            )
        )
        """,
        """
        CREATE UNIQUE INDEX hive_nodes_single_authority_idx
        ON hive_nodes(authoritative)
        WHERE authoritative = 1
        """,
        """
        CREATE TABLE hive_events (
            event_id TEXT PRIMARY KEY,
            origin_node_id TEXT NOT NULL REFERENCES hive_nodes(node_id),
            origin_sequence INTEGER NOT NULL CHECK (origin_sequence >= 1),
            event_type TEXT NOT NULL CHECK (
                event_type NOT IN (
                    'authority.transferred', 'external.action',
                    'node.enrolled', 'permission.granted', 'relay.unlock'
                )
            ),
            lifecycle_state TEXT NOT NULL CHECK (
                lifecycle_state IN (
                    'node_local', 'hive_pending', 'hive_shared',
                    'contested', 'consolidated', 'restricted', 'revoked'
                )
            ),
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            digest TEXT NOT NULL,
            UNIQUE(origin_node_id, origin_sequence)
        )
        """,
        """
        CREATE TABLE hive_event_parents (
            event_id TEXT NOT NULL REFERENCES hive_events(event_id),
            parent_id TEXT NOT NULL REFERENCES hive_events(event_id),
            PRIMARY KEY(event_id, parent_id),
            CHECK(event_id <> parent_id)
        )
        """,
        """
        CREATE INDEX hive_events_origin_sequence_idx
        ON hive_events(origin_node_id, origin_sequence)
        """,
        """
        CREATE INDEX hive_event_parents_parent_idx
        ON hive_event_parents(parent_id, event_id)
        """,
        """
        CREATE TRIGGER hive_nodes_prime_only_insert
        BEFORE INSERT ON hive_nodes
        WHEN NEW.identity_name <> 'Lantern-Prime'
          OR EXISTS (SELECT 1 FROM hive_nodes)
        BEGIN
            SELECT RAISE(ABORT, 'Gate 2H registry permits only Lantern-Prime');
        END
        """,
        """
        CREATE TRIGGER hive_nodes_no_update
        BEFORE UPDATE ON hive_nodes BEGIN
            SELECT RAISE(ABORT, 'hive enrollment records are immutable');
        END
        """,
        """
        CREATE TRIGGER hive_nodes_no_delete
        BEFORE DELETE ON hive_nodes BEGIN
            SELECT RAISE(ABORT, 'hive enrollment records are immutable');
        END
        """,
        """
        CREATE TRIGGER hive_events_no_update
        BEFORE UPDATE ON hive_events BEGIN
            SELECT RAISE(ABORT, 'hive events are immutable');
        END
        """,
        """
        CREATE TRIGGER hive_events_no_delete
        BEFORE DELETE ON hive_events BEGIN
            SELECT RAISE(ABORT, 'hive events are immutable');
        END
        """,
        """
        CREATE TRIGGER hive_event_parents_no_update
        BEFORE UPDATE ON hive_event_parents BEGIN
            SELECT RAISE(ABORT, 'hive causal links are immutable');
        END
        """,
        """
        CREATE TRIGGER hive_event_parents_no_delete
        BEFORE DELETE ON hive_event_parents BEGIN
            SELECT RAISE(ABORT, 'hive causal links are immutable');
        END
        """,
    ),
    8: (
        "DROP INDEX deletion_audit_completed_idx",
        "ALTER TABLE deletion_audit RENAME TO deletion_audit_v7",
        """
        CREATE TABLE deletion_audit (
            id TEXT PRIMARY KEY,
            target_kind TEXT NOT NULL CHECK (
                target_kind IN (
                    'claim', 'episode', 'preference', 'source', 'entity',
                    'cognitive_schema', 'deliberation'
                )
            ),
            retention_class TEXT NOT NULL CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            requested_by TEXT NOT NULL,
            reason_code TEXT NOT NULL CHECK (
                reason_code IN (
                    'owner_request', 'privacy', 'correction',
                    'expiry', 'obsolete'
                )
            ),
            protected_reconfirmed INTEGER NOT NULL CHECK (
                protected_reconfirmed IN (0, 1)
            ),
            process TEXT NOT NULL CHECK (process = 'MemoryStore.forget'),
            completed_at TEXT NOT NULL,
            backup_purge_due_at TEXT NOT NULL
        )
        """,
        "INSERT INTO deletion_audit SELECT * FROM deletion_audit_v7",
        "DROP TABLE deletion_audit_v7",
        """
        CREATE INDEX deletion_audit_completed_idx
        ON deletion_audit(completed_at)
        """,
        """
        CREATE TABLE mutation_receipts (
            receipt_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK (
                operation IN (
                    'claim.corrected', 'memory.forgotten',
                    'schema.created', 'schema.revised',
                    'deliberation.opened', 'deliberation.resolved'
                )
            ),
            target_kind TEXT NOT NULL CHECK (
                target_kind IN (
                    'claim', 'memory', 'cognitive_schema', 'deliberation'
                )
            ),
            outcome TEXT NOT NULL CHECK (outcome = 'committed'),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX mutation_receipts_operation_idx
        ON mutation_receipts(operation, created_at)
        """,
        """
        CREATE TRIGGER mutation_receipts_no_update
        BEFORE UPDATE ON mutation_receipts BEGIN
            SELECT RAISE(ABORT, 'mutation receipts are immutable');
        END
        """,
        """
        CREATE TRIGGER mutation_receipts_no_delete
        BEFORE DELETE ON mutation_receipts BEGIN
            SELECT RAISE(ABORT, 'mutation receipts are immutable');
        END
        """,
        """
        CREATE TABLE deliberations (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL CHECK (
                lifecycle_state IN ('open', 'resolved', 'deferred')
            ),
            retention_class TEXT NOT NULL CHECK (
                retention_class IN (
                    'working', 'temporary', 'ordinary', 'protected',
                    'required_record'
                )
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE deliberation_positions (
            id TEXT PRIMARY KEY,
            deliberation_id TEXT NOT NULL
                REFERENCES deliberations(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            interpretation TEXT NOT NULL,
            confidence REAL NOT NULL CHECK (
                confidence >= 0.0 AND confidence <= 1.0
            ),
            assumptions_json TEXT NOT NULL,
            disposition TEXT NOT NULL CHECK (
                disposition IN ('candidate', 'supported', 'rejected', 'unresolved')
            ),
            created_at TEXT NOT NULL,
            UNIQUE(deliberation_id, label)
        )
        """,
        """
        CREATE TABLE deliberation_evidence (
            position_id TEXT NOT NULL
                REFERENCES deliberation_positions(id) ON DELETE CASCADE,
            claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
            relation TEXT NOT NULL CHECK (
                relation IN ('supports', 'contradicts', 'qualifies')
            ),
            rationale TEXT NOT NULL,
            PRIMARY KEY(position_id, claim_id, relation)
        )
        """,
        """
        CREATE TABLE deliberation_resolutions (
            id TEXT PRIMARY KEY,
            deliberation_id TEXT NOT NULL
                REFERENCES deliberations(id) ON DELETE CASCADE,
            selected_labels_json TEXT NOT NULL,
            rationale TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX deliberations_state_idx
        ON deliberations(lifecycle_state, created_at)
        """,
        """
        CREATE INDEX deliberation_positions_deliberation_idx
        ON deliberation_positions(deliberation_id, label)
        """,
        """
        CREATE INDEX deliberation_evidence_claim_idx
        ON deliberation_evidence(claim_id, position_id)
        """,
        """
        CREATE INDEX deliberation_resolutions_deliberation_idx
        ON deliberation_resolutions(deliberation_id, created_at)
        """,
    ),
}


def _replaceable_indexes() -> dict[str, str]:
    indexes: dict[str, str] = {}
    for version in sorted(MIGRATIONS):
        for statement in MIGRATIONS[version]:
            normalized = statement.strip()
            words = normalized.split()
            if len(words) >= 3 and words[:2] == ["CREATE", "INDEX"]:
                indexes[words[2]] = normalized
    return dict(sorted(indexes.items()))


REPLACEABLE_INDEXES = _replaceable_indexes()

