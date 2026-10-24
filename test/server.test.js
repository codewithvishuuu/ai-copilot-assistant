const test = require('node:test');
const assert = require('node:assert/strict');

const { createApp, getCommunityAnswer } = require('../server');

test('getCommunityAnswer returns event guidance for event-related queries', () => {
  const answer = getCommunityAnswer('Any upcoming events this week?');
  assert.match(answer, /events are announced weekly/i);
});

test('POST /api/chat returns answer for valid message', async () => {
  const app = createApp();
  const server = app.listen(0);
  const { port } = server.address();

  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'How can I contribute?' })
    });

    assert.equal(response.status, 200);
    const payload = await response.json();
    assert.match(payload.answer, /contribute/i);
    assert.ok(payload.timestamp);
  } finally {
    server.close();
  }
});

test('POST /api/chat validates empty message', async () => {
  const app = createApp();
  const server = app.listen(0);
  const { port } = server.address();

  try {
    const response = await fetch(`http://127.0.0.1:${port}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '   ' })
    });

    assert.equal(response.status, 400);
    const payload = await response.json();
    assert.equal(payload.error, 'A non-empty message is required.');
  } finally {
    server.close();
  }
});
