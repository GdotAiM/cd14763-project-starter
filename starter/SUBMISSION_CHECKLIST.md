# Submission Checklist

Run these from `starter/` after `agentcore deploy` and `uv run setup_permissions.py`.
Save a screenshot (or the terminal output) of each one in `screenshots/`.

## 1. Test commands

- [ ] **Test 1 — Order Tracking**
  ```bash
  agentcore invoke '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}'
  ```
- [ ] **Test 2 — Refund Processing**
  ```bash
  agentcore invoke '{"prompt": "I want to return my Kindle Paperwhite (ORD-002). Please initiate a refund.", "customer_id": "CUST-123", "session_id": "t2"}'
  ```
- [ ] **Test 3 — Knowledge Base (RAG)**
  ```bash
  agentcore invoke '{"prompt": "What are the benefits of the Platinum loyalty tier?", "customer_id": "CUST-123", "session_id": "t3"}'
  ```
- [ ] **Test 4 — Memory (Long-Term)** (wait at least 30 seconds between A and B)
  ```bash
  agentcore invoke '{"prompt": "Hi, I am Jane. I prefer concise responses.", "customer_id": "CUST-123", "session_id": "s-A"}'
  agentcore invoke '{"prompt": "Do you remember my name and communication preference?", "customer_id": "CUST-123", "session_id": "s-B"}'
  ```
- [ ] **Test 5 — Loyalty Discount Calculation**
  ```bash
  agentcore invoke '{"prompt": "I am a Gold member with 4250 points. Calculate my discount on a $150 standard order.", "customer_id": "CUST-123", "session_id": "t5"}'
  ```
- [ ] **Test 6 — Browser Tool**
  ```bash
  agentcore invoke '{"prompt": "Go to https://www.udacity.com and tell me the page title.", "customer_id": "CUST-123", "session_id": "t6"}'
  ```

## 2. Screenshots

- [ ] Create a `screenshots/` folder and add one screenshot per test
  (e.g. `test1_order_tracking.png` ... `test6_browser.png`; Test 4 needs both sessions).

## 3. Reflection

- [ ] Write `REFLECTION.md` (200–400 words, in your own words) using these headings:

```markdown
# Reflection

> Write 200-400 words in your own words.

## Design decision

## Challenge

## Production consideration
```
