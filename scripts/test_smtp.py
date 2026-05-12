"""Quick SMTP smoke test — run once to verify credentials before wiring into the app."""

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

SMTP_HOST = "smtp.exmail.qq.com"
SMTP_PORT = 465
SMTP_USERNAME = "sembr@peakstone-labs.com"
SMTP_PASSWORD = input("Paste SMTP password/auth-code: ").strip()
TO = "test-x6knwyvfj@srv1.mail-tester.com"

msg = MIMEMultipart("alternative")
msg["Subject"] = "[sembr] SMTP smoke test"
msg["From"] = SMTP_USERNAME
msg["To"] = TO
msg["Date"] = formatdate(localtime=True)
msg["Message-Id"] = make_msgid(domain="peakstone-labs.com")
msg.attach(MIMEText("sembr SMTP test — plain text fallback", "plain"))
msg.attach(MIMEText("<h2>sembr SMTP test ✓</h2><p>HTML is working.</p>", "html"))

ctx = ssl.create_default_context()
with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
    server.login(SMTP_USERNAME, SMTP_PASSWORD)
    server.sendmail(SMTP_USERNAME, TO, msg.as_string())

print("Sent. Check kaihuahuang@outlook.com.")
