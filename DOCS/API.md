# nanobot Manus Extension - Web API Documentation

The nanobot Manus Extension provides a Web API and real-time event streaming infrastructure designed to support "Manus-style" UIs. It is implemented as a separate extension that does not modify the core `nanobot` code.

## Running the Manus Gateway

To use the enhanced Web API and real-time streaming, run the Manus gateway instead of the standard nanobot gateway:

```bash
python manus/cli.py gateway
```

Optional flags:
* `--web-port PORT`: Specify the port for the Web API (default: 8000).
* `-p, --port PORT`: Specify the nanobot gateway port (default: 18790).

---

## Base URL
The default base URL for the Web API is `http://localhost:8000`.

## REST Endpoints

### 1. Send Message
* **URL:** `/api/messages`
* **Method:** `POST`
* **Body:**
  ```json
  {
    "content": "Hello",
    "chat_id": "session_123",
    "sender_id": "user_456"
  }
  ```

### 2. Get Session History
* **URL:** `/api/sessions/{session_id}`
* **Method:** `GET`

### 3. Health Check
* **URL:** `/api/health`
* **Method:** `GET`

---

## Real-time Event Streaming (SSE)

Connect to the streaming endpoint to receive granular updates about the agent's progress.

* **URL:** `/api/events/{chat_id}`

### Event Format
Events are sent as JSON in the `data` field. To maintain compatibility with the core `nanobot` event types, **granular event information is nested within the `metadata` field.**

```json
{
  "event_type": "string",  // Always present (defaults to "message")
  "content": "string",
  "metadata": {
    "event_type": "string", // Detailed event: thinking, tool_call, tool_result, message
    "iteration": 1,
    "tool": "tool_name",
    "tool_call_id": "id",
    "arguments": {},
    "is_reasoning": true,
    "timestamp": "ISO-8601 string"
  },
  "timestamp": "string",
  "chat_id": "string"
}
```

### Granular Event Types (in `metadata.event_type`)

1. **`thinking`**: Agent is processing or providing internal reasoning.
2. **`tool_call`**: Agent has initiated a tool execution.
3. **`tool_result`**: A tool execution has completed and returned a result.
4. **`message`**: A final response or message fragment.

### Example SSE Listener (JavaScript)

```javascript
const eventSource = new EventSource(`http://localhost:8000/api/events/my_session`);

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  const type = data.metadata.event_type || data.event_type;

  console.log(`Event [${type}]:`, data);

  if (type === 'thinking' && data.metadata.is_reasoning) {
    console.log('Agent Thoughts:', data.content);
  }
};
```
