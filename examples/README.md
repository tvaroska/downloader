# REST API Downloader Examples

This directory contains practical examples demonstrating how to use the REST API Downloader service.

## Prerequisites

1. **Running Server**: Start the downloader service using Docker or locally:

   ```bash
   # Using Docker (recommended)
   docker build -t downloader .
   docker run -p 8000:80 downloader
   
   # Or locally with uv
   uv run python run.py
   ```

2. **Python Dependencies**: Install required packages for examples:

   ```bash
   pip install httpx asyncio aiofiles matplotlib
   ```

## Examples Overview

### Basic Usage Examples

- **[basic_usage.py](basic_usage.py)** - Simple API usage patterns
- **[content_formats.py](content_formats.py)** - Different response formats demonstration

### Performance and Concurrency

- **[concurrent_pdf_requests.py](concurrent_pdf_requests.py)** - Multiple concurrent PDF generation
- **[load_testing.py](load_testing.py)** - Load testing scenarios
- **[performance_comparison.py](performance_comparison.py)** - Performance comparison across formats

### Advanced Usage

- **[batch_simulation.py](batch_simulation.py)** - Simulates batch processing (future feature)
- **[error_handling.py](error_handling.py)** - Comprehensive error handling examples
- **[monitoring_example.py](monitoring_example.py)** - Health monitoring and metrics

## Quick Start

1. **Start the server**:
   ```bash
   docker run -p 8000:80 downloader
   ```

2. **Test basic functionality**:
   ```bash
   python examples/basic_usage.py
   ```

3. **Run concurrent PDF test**:
   ```bash
   python examples/concurrent_pdf_requests.py
   ```

## Example Categories

### Learning Path
1. Start with `basic_usage.py` to understand API basics
2. Explore `content_formats.py` for different response types
3. Test performance with `concurrent_pdf_requests.py`
4. Handle edge cases with `error_handling.py`

### Production Integration
- Use `load_testing.py` to validate performance under load
- Implement patterns from `error_handling.py` in production code
- Monitor service health using `monitoring_example.py`

## Common Use Cases

### Content Extraction
```python
import httpx

# Extract article text
response = httpx.get("http://localhost:8000/https://news.ycombinator.com", 
                    headers={"Accept": "text/plain"})
article_text = response.text
```

### PDF Generation
```python
# Generate PDF from webpage
pdf_response = httpx.get("http://localhost:8000/https://example.com",
                        headers={"Accept": "application/pdf"})
with open("output.pdf", "wb") as f:
    f.write(pdf_response.content)
```

### Structured Data
```python
# Get JSON with metadata
json_response = httpx.get("http://localhost:8000/https://httpbin.org/json",
                         headers={"Accept": "application/json"})
data = json_response.json()
```

## Configuration

Most examples use these default settings:
- **Server URL**: `http://localhost:8000`
- **Timeout**: 30 seconds
- **Concurrent requests**: 10 (adjustable)

Modify the `BASE_URL` variable in each example to match your server configuration.

## Troubleshooting

### Common Issues

1. **Connection refused**: Ensure the server is running on the correct port
2. **PDF generation fails**: Check that Playwright browsers are installed in container
3. **Timeout errors**: Increase timeout for large files or slow URLs

### Server Health Check
```bash
curl http://localhost:8000/health
```

Should return:
```json
{
  "status": "healthy",
  "version": "0.0.1",
  "auth_enabled": false,
  "auth_methods": null
}
```

## Contributing Examples

When adding new examples:

1. Follow the naming convention: `descriptive_name.py`
2. Include comprehensive docstrings and comments
3. Add error handling and logging
4. Update this README with the new example
5. Test with both successful and error scenarios

## Support

- **Documentation**: See [API Reference](../doc/api-reference.md)
- **Issues**: Report problems via GitHub issues
- **Architecture**: Review [Technical Architecture](../product/architecture.md)