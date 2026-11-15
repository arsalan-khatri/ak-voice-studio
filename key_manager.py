"""Local key management and bounded, circular failover. Never expose raw keys."""
import json
import os
import threading
import uuid
from pathlib import Path
from fastapi import HTTPException


class KeyStore:
    def __init__(self, path, env_key=''):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.events = []
        self.revision = 0
        self.items = []
        self.active = None
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.items = data['keys']
            self.active = data.get('active')
        elif env_key.strip():
            self.items = [{'id': uuid.uuid4().hex, 'label': 'Existing .env key', 'key': env_key.strip()}]
            self.active = self.items[0]['id']

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'keys': self.items, 'active': self.active}), encoding='utf-8')
        os.replace(temp, self.path)

    def event(self, message):
        self.revision += 1
        self.events.append({'id': self.revision, 'message': message})
        self.events = self.events[-30:]

    def public(self):
        with self.lock:
            return {'keys': [{'id': k['id'], 'label': k['label'], 'masked': '••••' + k['key'][-4:],
                              'status': k.get('status', 'Ready')} for k in self.items],
                    'active': self.active, 'events': list(self.events), 'revision': self.revision}

    def ordered(self):
        with self.lock:
            if not self.items:
                raise HTTPException(400, 'Settings mein + dabakar Gemini API key add karein.')
            index = next((i for i, k in enumerate(self.items) if k['id'] == self.active), 0)
            return [dict(k) for k in self.items[index:] + self.items[:index]]

    def add(self, key, label=''):
        key = key.strip()
        if not key or len(key) > 256 or any(c.isspace() for c in key):
            raise HTTPException(400, 'Valid API key paste karein (spaces ke baghair).')
        with self.lock:
            if any(k['key'] == key for k in self.items):
                raise HTTPException(400, 'Yeh key pehle se added hai.')
            item = {'id': uuid.uuid4().hex, 'label': label.strip()[:60] or f'Key {len(self.items) + 1}', 'key': key}
            self.items.append(item)
            if not self.active:
                self.active = item['id']
            self.save()
            self.event(f"{item['label']} added.")
            return self.public()

    def select(self, key_id):
        with self.lock:
            item = next((k for k in self.items if k['id'] == key_id), None)
            if item is None:
                raise HTTPException(404, 'Key nahi mili.')
            self.active = key_id
            self.save()
            self.event(f"Manual switch: {item['label']} active. Nayi requests is key se jayengi.")
            return self.public()

    def remove(self, key_id):
        with self.lock:
            if not any(k['id'] == key_id for k in self.items):
                raise HTTPException(404, 'Key nahi mili.')
            self.items = [k for k in self.items if k['id'] != key_id]
            if self.active == key_id:
                self.active = self.items[0]['id'] if self.items else None
            self.save()
            return self.public()

    def mark(self, key_id, status):
        with self.lock:
            for k in self.items:
                if k['id'] == key_id:
                    k['status'] = status

    def advance(self, failed, target, reason):
        with self.lock:
            # Do not override a user's manual selection during an in-flight request.
            if self.active == failed['id'] and any(k['id'] == target['id'] for k in self.items):
                self.active = target['id']
                self.save()
            self.event(f"Auto switch: {failed['label']} → {target['label']} ({reason}). Wahi request dobara bhej rahe hain.")


def failover_reason(response):
    try:
        error = response.json().get('error', {})
        message = str(error.get('message', '')).lower()
        reasons = {d.get('reason') for d in error.get('details', []) if isinstance(d, dict)}
    except (ValueError, AttributeError, TypeError):
        message, reasons = '', set()
    if response.status_code == 429:
        return 'Quota / rate limit'
    if response.status_code == 401:
        return 'Invalid / expired key'
    if response.status_code in (400, 403) and (
        reasons & {'API_KEY_INVALID', 'API_KEY_EXPIRED', 'API_KEY_SERVICE_BLOCKED', 'API_KEY_HTTP_REFERRER_BLOCKED', 'API_KEY_IP_ADDRESS_BLOCKED'}
        or any(t in message for t in ('api key not valid', 'api key expired', 'api key has expired', 'api key was reported as leaked', 'api key is invalid'))
    ):
        return 'Invalid / expired / restricted key'
    return None
