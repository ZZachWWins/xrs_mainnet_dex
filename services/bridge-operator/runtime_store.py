"""Immutable public transaction plans/context in the settlement journal DB.

Only canonical public unsigned plans, signed public transactions and read
evidence are stored. No private key input or key paths are accepted here.
"""
import json
from journal import canonical, require


class RuntimeStore:
    def __init__(self, journal):
        self.journal = journal
        journal.db.execute('''CREATE TABLE IF NOT EXISTS settlement_plans(
          intent_id TEXT NOT NULL REFERENCES settlement_intents(id),stage TEXT NOT NULL,
          plan_json TEXT NOT NULL,created_at INTEGER NOT NULL,
          PRIMARY KEY(intent_id,stage))''')
        journal.db.execute('''CREATE TABLE IF NOT EXISTS settlement_source_recovery(
          intent_id TEXT PRIMARY KEY REFERENCES settlement_intents(id),
          chain TEXT NOT NULL,transaction_id TEXT NOT NULL,transaction_base64 TEXT NOT NULL,
          resolved INTEGER NOT NULL DEFAULT 0,UNIQUE(chain,transaction_id))''')

    def get(self, intent_id, stage):
        row = self.journal.db.execute('SELECT plan_json FROM settlement_plans WHERE intent_id=? AND stage=?',
                                      (intent_id, stage)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, intent_id, stage, plan, lease):
        encoded = canonical(plan)
        require(len(encoded.encode()) <= 2_000_000, 'Prepared context exceeds storage bound')
        with self.journal._atomic():
            self.journal._lease(intent_id, lease)
            row, intent = self.journal._intent(intent_id)
            require(row['terminal'] is None, 'Route already terminal')
            self.journal._previous(row, intent, stage)
            prior = self.get(intent_id, stage)
            if prior is not None:
                require(canonical(prior) == encoded, 'Original prepared message/context is immutable')
                return prior
            require(not self.journal.processing_state()['paused'], 'Processing paused')
            self.journal.db.execute('INSERT INTO settlement_plans VALUES(?,?,?,?)',
                (intent_id, stage, encoded, self.journal._now()))
            self.journal._event('plan_frozen', intent_id, {'stage': stage,
                'message_sha256': plan['prepared'].get('message_sha256')})
        return plan

    def payload(self, intent_id, stage):
        row = self.journal.db.execute('SELECT transaction_id,transaction_base64 FROM settlement_payloads WHERE intent_id=? AND stage=?',
                                     (intent_id, stage)).fetchone()
        return dict(row) if row else None

    def recovery_source(self, intent_id):
        row = self.journal.db.execute('SELECT * FROM settlement_source_recovery WHERE intent_id=?', (intent_id,)).fetchone()
        return dict(row) if row else None

    def put_recovery_source(self, intent_id, chain, transaction_id, encoded):
        with self.journal._atomic():
            prior = self.recovery_source(intent_id)
            if prior:
                require((prior['chain'], prior['transaction_id'], prior['transaction_base64']) ==
                        (chain, transaction_id, encoded), 'Replacement recovery source forbidden')
            else:
                self.journal.db.execute('INSERT INTO settlement_source_recovery VALUES(?,?,?,?,0)',
                    (intent_id, chain, transaction_id, encoded))
                self.journal._event('late_original_recorded', intent_id, {'chain': chain, 'transaction_id': transaction_id})

    def finish_recovery_source(self, intent_id):
        with self.journal._atomic():
            self.journal.db.execute('UPDATE settlement_source_recovery SET resolved=1 WHERE intent_id=?', (intent_id,))

    def pending_intents(self, limit=100):
        require(type(limit) is int and 1 <= limit <= 100, 'Invalid recovery limit')
        return [r[0] for r in self.journal.db.execute('SELECT id FROM settlement_intents WHERE terminal IS NULL ORDER BY accepted_at,id LIMIT ?', (limit,))]
