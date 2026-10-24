const express = require('express');
const path = require('path');

function getCommunityAnswer(message) {
  const input = message.toLowerCase();

  if (input.includes('event') || input.includes('meetup')) {
    return 'Community events are announced weekly. Check the events channel and RSVP to join upcoming sessions.';
  }

  if (input.includes('support') || input.includes('help')) {
    return 'For support, post your question with context and screenshots in the help forum so mentors can assist quickly.';
  }

  if (input.includes('contribute') || input.includes('volunteer')) {
    return 'You can contribute by answering questions, sharing resources, and helping moderate discussions respectfully.';
  }

  if (input.includes('join') || input.includes('member')) {
    return 'To join the community, complete the signup form and introduce yourself in the welcome thread.';
  }

  if (input.includes('rule') || input.includes('guideline')) {
    return 'Please follow the community guidelines: be respectful, stay on topic, and avoid sharing sensitive data.';
  }

  return 'Thanks for your question. Share a bit more detail and the Community Copilot will guide you with the best next steps.';
}

function createApp() {
  const app = express();

  app.use(express.json());
  app.use(express.static(path.join(__dirname, 'public')));

  app.post('/api/chat', (req, res) => {
    const { message } = req.body || {};

    if (typeof message !== 'string' || !message.trim()) {
      return res.status(400).json({
        error: 'A non-empty message is required.'
      });
    }

    const answer = getCommunityAnswer(message.trim());

    return res.json({
      answer,
      timestamp: new Date().toISOString()
    });
  });

  return app;
}

if (require.main === module) {
  const port = process.env.PORT || 3000;
  createApp().listen(port, () => {
    console.log(`Community Copilot running on http://localhost:${port}`);
  });
}

module.exports = {
  createApp,
  getCommunityAnswer
};
