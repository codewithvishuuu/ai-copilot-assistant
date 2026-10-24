# ai-copilot-assistant

An AI-powered Community Copilot web app with a clean chat interface and backend API for community queries.

## Run locally

```bash
npm install
npm start
```

Open `http://localhost:3000`.

## API

`POST /api/chat`

Request body:

```json
{ "message": "How can I contribute?" }
```

Response body:

```json
{
  "answer": "You can contribute by answering questions, sharing resources, and helping moderate discussions respectfully.",
  "timestamp": "2026-01-01T00:00:00.000Z"
}
```

## Test

```bash
npm test
```
