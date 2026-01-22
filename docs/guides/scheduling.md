# Scheduling Guide

**Last Updated:** 2026-01-21

This guide covers how to set up and manage scheduled downloads using the REST API Downloader's scheduling API.

## Overview

The scheduling API enables automated, recurring downloads using cron expressions. Common use cases include:

- **Daily report collection** - Download reports at fixed times
- **Content monitoring** - Track changes to web pages
- **Data archiving** - Periodically backup web content
- **Feed aggregation** - Collect content from multiple sources on a schedule

Schedules are persisted in Redis and survive service restarts.

## Prerequisites

### Redis Required

The scheduler requires Redis for persistent job storage. Configure Redis:

```bash
# Set Redis connection
export REDIS_URI="redis://localhost:6379"

# Or with authentication
export REDIS_URI="redis://:password@redis-host:6379/0"
```

Without Redis, the scheduler will fall back to in-memory storage (not recommended for production as schedules are lost on restart).

### Start the Service

```bash
# Start with Redis
uv run uvicorn downloader.main:app --host 0.0.0.0 --port 8000
```

## Creating Schedules

### Basic Schedule

Create a schedule to download a URL daily at 9 AM:

```bash
curl -X POST http://localhost:8000/schedules \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "name": "Daily News",
    "url": "https://news.example.com",
    "cron_expression": "0 9 * * *"
  }'
```

### With Output Format

Specify the output format for downloaded content:

```bash
curl -X POST http://localhost:8000/schedules \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "name": "Daily Report (Markdown)",
    "url": "https://example.com/report",
    "cron_expression": "0 9 * * *",
    "format": "markdown"
  }'
```

**Supported formats:** `text`, `html`, `markdown`, `pdf`, `json`, `raw`

### With Custom Headers

Include authentication or custom headers:

```bash
curl -X POST http://localhost:8000/schedules \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "name": "Authenticated Report",
    "url": "https://api.example.com/private-report",
    "cron_expression": "0 9 * * 1-5",
    "format": "json",
    "headers": {
      "Authorization": "Bearer report-api-token",
      "X-Custom-Header": "value"
    }
  }'
```

### Python Example

```python
import requests

API_URL = "http://localhost:8000"
API_KEY = "your-api-key"  # pragma: allowlist secret

# Create a daily schedule
response = requests.post(
    f"{API_URL}/schedules",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}"
    },
    json={
        "name": "Daily News Digest",
        "url": "https://news.example.com",
        "cron_expression": "0 8 * * *",
        "format": "markdown"
    }
)

schedule = response.json()
print(f"Created schedule: {schedule['id']}")
print(f"Next run: {schedule['next_run_time']}")
```

### JavaScript Example

```javascript
const API_URL = "http://localhost:8000";
const API_KEY = "your-api-key";  // pragma: allowlist secret

const response = await fetch(`${API_URL}/schedules`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${API_KEY}`
  },
  body: JSON.stringify({
    name: "Hourly Price Check",
    url: "https://api.example.com/prices",
    cron_expression: "0 * * * *",
    format: "json"
  })
});

const schedule = await response.json();
console.log(`Schedule ID: ${schedule.id}`);
console.log(`Next run: ${schedule.next_run_time}`);
```

## Cron Expression Syntax

Schedules use standard 5-field UNIX cron format:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, 0=Sunday)
│ │ │ │ │
* * * * *
```

### Common Patterns

| Use Case | Expression | Description |
|----------|-----------|-------------|
| Every minute | `* * * * *` | Runs every minute (use sparingly) |
| Every 15 minutes | `*/15 * * * *` | At 0, 15, 30, 45 minutes |
| Hourly | `0 * * * *` | At the start of every hour |
| Every 6 hours | `0 */6 * * *` | At midnight, 6 AM, noon, 6 PM |
| Daily at 9 AM | `0 9 * * *` | Every day at 9:00 AM |
| Daily at midnight | `0 0 * * *` | Every day at 12:00 AM |
| Weekdays at 9 AM | `0 9 * * 1-5` | Monday through Friday at 9:00 AM |
| Weekends at 10 AM | `0 10 * * 0,6` | Saturday and Sunday at 10:00 AM |
| Weekly on Monday | `0 9 * * 1` | Every Monday at 9:00 AM |
| Weekly on Sunday | `0 0 * * 0` | Every Sunday at midnight |
| First of month | `0 0 1 * *` | 1st day of each month at midnight |
| Quarterly | `0 0 1 1,4,7,10 *` | First day of each quarter |

### Operators

| Operator | Example | Description |
|----------|---------|-------------|
| `*` | `* * * * *` | Any value |
| `*/n` | `*/15 * * * *` | Every n units |
| `n-m` | `0 9-17 * * *` | Range (9 AM to 5 PM) |
| `n,m,o` | `0 0 1,15 * *` | Specific values (1st and 15th) |

### Validation

Invalid cron expressions return a 400 error:

```bash
# Invalid: only 4 fields
curl -X POST http://localhost:8000/schedules \
  -H "Content-Type: application/json" \
  -d '{"name": "Test", "url": "https://example.com", "cron_expression": "0 9 * *"}'

# Response:
# {"detail": {"success": false, "error": "Invalid cron expression: Wrong number of fields", "error_type": "validation_error"}}
```

## Managing Schedules

### List All Schedules

```bash
curl http://localhost:8000/schedules \
  -H "Authorization: Bearer your-api-key"
```

### Get Schedule Details

```bash
curl http://localhost:8000/schedules/550e8400-e29b-41d4-a716-446655440000 \
  -H "Authorization: Bearer your-api-key"
```

### Delete a Schedule

```bash
curl -X DELETE http://localhost:8000/schedules/550e8400-e29b-41d4-a716-446655440000 \
  -H "Authorization: Bearer your-api-key"
```

## Monitoring Executions

### View Execution History

Get the history of past executions for a schedule:

```bash
curl "http://localhost:8000/schedules/550e8400-e29b-41d4-a716-446655440000/history" \
  -H "Authorization: Bearer your-api-key"
```

### Paginate Results

```bash
# Get second page of 10 results
curl "http://localhost:8000/schedules/550e8400-e29b-41d4-a716-446655440000/history?limit=10&offset=10" \
  -H "Authorization: Bearer your-api-key"
```

### Execution Status Values

| Status | Description |
|--------|-------------|
| `pending` | Execution is queued |
| `running` | Currently downloading |
| `completed` | Finished successfully |
| `failed` | Failed after all retry attempts |

### Interpreting History

```json
{
  "executions": [
    {
      "execution_id": "abc123",
      "status": "completed",
      "started_at": "2026-01-21T09:00:00Z",
      "completed_at": "2026-01-21T09:00:02Z",
      "duration": 2.5,
      "success": true,
      "content_size": 15234,
      "attempt": 1
    },
    {
      "execution_id": "def456",
      "status": "failed",
      "started_at": "2026-01-20T09:00:00Z",
      "completed_at": "2026-01-20T09:00:35Z",
      "duration": 35.2,
      "success": false,
      "error_message": "Connection timeout after 3 attempts",
      "attempt": 3
    }
  ]
}
```

Key fields:
- **duration**: Execution time in seconds
- **content_size**: Downloaded content size in bytes (null if failed)
- **attempt**: Which retry attempt this was (1-3)
- **error_message**: Error details if failed

## Retry Behavior

Failed downloads are automatically retried:

1. **First attempt** - Immediate execution
2. **Second attempt** - After 5 seconds
3. **Third attempt** - After 15 seconds (20s total)
4. **Final failure** - After 30 seconds (50s total)

After 3 failed attempts, the execution is marked as `failed` in history and no further retries occur for that scheduled run. The next scheduled execution will start fresh with attempt 1.

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URI` | - | Redis connection URI (required for persistence) |
| `SCHEDULER_JOB_STORE_TYPE` | `redis` | Storage backend: `redis` or `memory` |
| `SCHEDULER_MAX_WORKERS` | `10` | Maximum concurrent scheduled jobs |
| `SCHEDULER_MISFIRE_GRACE_TIME` | `60` | Seconds late a job can still run |
| `SCHEDULER_COALESCE` | `true` | Combine multiple missed runs into one |

### Example Configuration

```bash
# Production configuration
export REDIS_URI="redis://redis:6379/0"
export SCHEDULER_JOB_STORE_TYPE="redis"
export SCHEDULER_MAX_WORKERS="20"
export SCHEDULER_MISFIRE_GRACE_TIME="120"
```

### Misfire Handling

If the service is down when a job should run:
- **Within grace time**: Job runs immediately when service restarts
- **Beyond grace time**: Job is skipped (misfired)
- **Coalesce enabled**: Multiple missed runs execute only once

## Best Practices

### Rate Limiting Considerations

- Avoid scheduling too many jobs at the same minute
- Spread jobs across different times to reduce load spikes
- Consider the target website's rate limits

```bash
# Bad: All at 9:00 AM
"0 9 * * *"
"0 9 * * *"
"0 9 * * *"

# Better: Staggered
"0 9 * * *"
"5 9 * * *"
"10 9 * * *"
```

### Error Handling

- Monitor execution history for failures
- Set up alerts for repeated failures
- Check `error_message` for troubleshooting

### Naming Conventions

Use descriptive names that include:
- Source or target
- Frequency
- Purpose

```json
{
  "name": "HN-Daily-TopStories",
  "name": "Weather-Hourly-NYC",
  "name": "Prices-Weekday-9AM"
}
```

## Troubleshooting

### Schedule Not Running

1. **Check Redis connection**: Ensure `REDIS_URI` is set correctly
2. **Verify scheduler is running**: Check service logs for scheduler startup
3. **Check next_run_time**: Ensure it's in the future
4. **Review cron expression**: Validate syntax

### Execution Failures

1. **Check history**: Look at `error_message` in execution history
2. **Test URL manually**: Use the regular download endpoint first
3. **Verify headers**: Ensure authentication tokens are valid
4. **Check network**: Confirm target URL is accessible

### Missing Executions

1. **Check misfire_grace_time**: Increase if service restarts are common
2. **Verify Redis persistence**: Ensure Redis data isn't being cleared
3. **Check for coalescing**: Multiple missed runs may combine into one

### Service Unavailable (503)

The scheduler requires Redis. If you see 503 errors:

```bash
# Check Redis is running
redis-cli ping

# Verify connection URI
echo $REDIS_URI
```

## API Quick Reference

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Create schedule | POST | `/schedules` |
| List schedules | GET | `/schedules` |
| Get schedule | GET | `/schedules/{id}` |
| Get history | GET | `/schedules/{id}/history` |
| Delete schedule | DELETE | `/schedules/{id}` |

For complete API documentation, see the [API Reference](../api/api-reference.md#scheduled-downloads).
