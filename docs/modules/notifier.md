# notifier

> **Status**: pending review

Push delivery layer. Sends LLM-generated summaries to configured channels (Telegram, Discord, Slack, email). Handles message splitting at channel-specific length limits and updates `notification_log` state (`pending → sent / failed → dead`).

<!-- Review and fill in this page before opening the module to contributors. -->
