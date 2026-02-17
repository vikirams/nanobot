# nanobot Web API Documentation

The nanobot Web API provides a RESTful interface and real-time event streaming via Server-Sent Events (SSE). This infrastructure is designed to support modern, "Manus-style" UIs that require real-time visualization of agent thinking, tool usage, and responses.

## Base URL
The default base URL is `http://localhost:8000`. This can be configured in `~/.nanobot/config.json`.

## REST Endpoints

### 1. Send Message
Send a message to the agent.

* **URL:** `/api/messages`
* **Method:** `POST`
* **Content-Type:** `application/json`
* **Body:**
  ```json
  {
    "content": "Hello, what can you do?",
    "chat_id": "session_123",
    "sender_id": "user_456",
    "metadata": {}
  }
  ```
* **Response:**
  ```json
  {
    "status": "sent",
    "chat_id": "session_123"
  }
  ```

### 2. Get Session History
Retrieve the conversation history for a specific session.

* **URL:** `/api/sessions/{session_id}`
* **Method:** `GET`
* **Response:**
  ```json
  {
    "session_id": "session_123",
    "messages": [
      {
        "role": "user",
        "content": "Hello",
        "timestamp": "2023-10-27T10:00:00"
      },
      {
        "role": "assistant",
        "content": "Hi there!",
        "timestamp": "2023-10-27T10:00:05",
        "tools_used": []
      }
    ],
    "metadata": {}
  }
  ```

### 3. Health Check
Check if the API server is running.

* **URL:** `/api/health`
* **Method:** `GET`
* **Response:** `{"status": "ok"}`

---

## Real-time Event Streaming (SSE)

Real-time updates are provided via Server-Sent Events. To receive updates, connect to the streaming endpoint for a specific `chat_id`.

* **URL:** `/api/events/{chat_id}`
* **Method:** `GET`
* **Headers:** `Accept: text/event-stream`

### Event Format
All events are sent as JSON strings in the `data` field of the SSE message.

```json
{
  "event_type": "string",
  "content": "string",
  "metadata": "object",
  "timestamp": "ISO-8601 string",
  "chat_id": "string"
}
```

### Event Types

#### 1. `connected`
Sent immediately upon successful connection.
* **Content:** Empty
* **Metadata:** Contains `chat_id`

#### 2. `thinking`
Sent when the agent starts a new iteration or provides internal reasoning.
* **Content:** Reasoning text (if available)
* **Metadata:**
  - `iteration`: Current iteration number
  - `is_reasoning`: `true` if content contains the agent's internal "thought" process

#### 3. `tool_call`
Sent when the agent initiates a tool execution.
* **Content:** Empty
* **Metadata:**
  - `tool`: Name of the tool
  - `arguments`: Arguments passed to the tool
  - `tool_call_id`: Unique ID for this tool call

#### 4. `tool_result`
Sent when a tool execution completes.
* **Content:** The result of the tool execution (stringified)
* **Metadata:**
  - `tool`: Name of the tool
  - `tool_call_id`: ID matching the previous `tool_call`

#### 5. `message`
Sent when the agent provides a final response fragment or complete message.
* **Content:** The message text
* **Metadata:** Any additional channel-specific data

---

## Example Implementation (JavaScript)

```javascript
const chatId = 'my_session';

// 1. Listen for real-time events
const eventSource = new EventSource(`http://localhost:8000/api/events/${chatId}`);

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Received event:', data);

  switch(data.event_type) {
    case 'thinking':
      showSpinner(data.content);
      break;
    case 'tool_call':
      logToolUsage(data.metadata.tool, data.metadata.arguments);
      break;
    case 'message':
      appendMessage('assistant', data.content);
      break;
  }
};

// 2. Send a message
await fetch('http://localhost:8000/api/messages', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    content: 'Find the weather in Tokyo',
    chat_id: chatId
  })
});
```
