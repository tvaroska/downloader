"""API endpoints for the downloader service."""

import base64
import logging
import re
import json
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Response, Path, Header, Request
from pydantic import BaseModel
from bs4 import BeautifulSoup

from .validation import validate_url, URLValidationError
from .http_client import get_client, HTTPClientError, HTTPTimeoutError, DownloadError

logger = logging.getLogger(__name__)

router = APIRouter()


class ErrorResponse(BaseModel):
    """Response model for errors."""

    success: bool = False
    error: str
    error_type: str


def parse_accept_header(accept_header: Optional[str]) -> str:
    """
    Parse Accept header to determine response format.
    
    Args:
        accept_header: The Accept header value
        
    Returns:
        Format string: 'text', 'html', 'markdown', or 'raw'
    """
    if not accept_header:
        return "text"
    
    accept_header = accept_header.lower()
    
    # Check for specific formats in order of preference
    if "text/plain" in accept_header:
        return "text"
    elif "text/html" in accept_header:
        return "html"
    elif "text/markdown" in accept_header or "text/x-markdown" in accept_header:
        return "markdown"
    elif "application/json" in accept_header:
        return "json"
    else:
        return "raw"


def convert_content_to_text(content: bytes, content_type: str) -> str:
    """
    Convert content to plain text, extracting main article content from HTML.
    
    Args:
        content: Raw content bytes
        content_type: Original content type
        
    Returns:
        Plain text representation with article content extracted
    """
    try:
        # Try to decode as text
        text = content.decode('utf-8', errors='ignore')
        
        # If it's HTML, use BeautifulSoup to extract article content
        if 'html' in content_type.lower():
            soup = BeautifulSoup(text, 'lxml')
            
            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 
                               'aside', 'menu', 'form', 'iframe', 'noscript']):
                element.decompose()
            
            # Try to find main content in common article containers
            main_content = None
            for selector in ['article', 'main', '[role="main"]', '.content', 
                           '.post-content', '.entry-content', '.article-content']:
                main_content = soup.select_one(selector)
                if main_content:
                    break
            
            # If no main content container found, use body or entire document
            if not main_content:
                main_content = soup.find('body') or soup
            
            # Extract text with proper spacing
            text = main_content.get_text(separator=' ', strip=True)
            
            # Clean up excessive whitespace
            text = re.sub(r'\s+', ' ', text).strip()
        
        return text
    except Exception:
        return content.decode('utf-8', errors='replace')


def convert_content_to_markdown(content: bytes, content_type: str) -> str:
    """
    Convert content to markdown format, extracting main article content from HTML.
    
    Args:
        content: Raw content bytes
        content_type: Original content type
        
    Returns:
        Markdown representation with article content extracted
    """
    try:
        text = content.decode('utf-8', errors='ignore')
        
        # If it's HTML, use BeautifulSoup for better markdown conversion
        if 'html' in content_type.lower():
            soup = BeautifulSoup(text, 'lxml')
            
            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'header', 'footer', 
                               'aside', 'menu', 'form', 'iframe', 'noscript']):
                element.decompose()
            
            # Try to find main content in common article containers
            main_content = None
            for selector in ['article', 'main', '[role="main"]', '.content', 
                           '.post-content', '.entry-content', '.article-content']:
                main_content = soup.select_one(selector)
                if main_content:
                    break
            
            # If no main content container found, use body or entire document
            if not main_content:
                main_content = soup.find('body') or soup
            
            # Convert to markdown
            markdown_parts = []
            
            for element in main_content.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'a', 'ul', 'ol', 'li']):
                if element.name.startswith('h'):
                    level = int(element.name[1])
                    markdown_parts.append('#' * level + ' ' + element.get_text(strip=True))
                elif element.name == 'p':
                    text_content = element.get_text(strip=True)
                    if text_content:
                        markdown_parts.append(text_content)
                elif element.name == 'a' and element.get('href'):
                    link_text = element.get_text(strip=True)
                    href = element.get('href')
                    if link_text and href:
                        markdown_parts.append(f'[{link_text}]({href})')
                elif element.name in ['ul', 'ol']:
                    for li in element.find_all('li', recursive=False):
                        li_text = li.get_text(strip=True)
                        if li_text:
                            prefix = '- ' if element.name == 'ul' else '1. '
                            markdown_parts.append(prefix + li_text)
            
            # If no structured content found, fall back to simple text extraction
            if not markdown_parts:
                text = main_content.get_text(separator='\n', strip=True)
                text = re.sub(r'\n\s*\n+', '\n\n', text)
                return text.strip()
            
            # Join markdown parts with appropriate spacing
            text = '\n\n'.join(markdown_parts)
            
            # Clean up excessive whitespace
            text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
            text = text.strip()
        
        return text
    except Exception:
        return content.decode('utf-8', errors='replace')


@router.get("/{url:path}")
async def download_url(
    url: str = Path(..., description="The URL to download"),
    accept: Optional[str] = Header(None, description="Accept header for content negotiation"),
    request: Request = None,
) -> Response:
    """
    Download content from a single URL with content negotiation.

    Args:
        url: The URL to download from
        accept: Accept header for content negotiation
        request: FastAPI request object

    Returns:
        Response with content in requested format

    Raises:
        HTTPException: For various error conditions
        
    Content Negotiation via Accept Header:
    - text/plain: Returns plain text (HTML tags stripped)
    - text/html: Returns original HTML content
    - text/markdown: Returns markdown conversion of HTML
    - application/json: Returns JSON with base64 content and metadata
    - */*: Returns raw bytes with original content-type
    """
    try:
        # Validate and sanitize URL
        validated_url = validate_url(url)
        logger.info(f"Processing download request for: {validated_url}")

        # Get HTTP client and download content
        client = await get_client()
        content, metadata = await client.download(validated_url)

        # Determine response format from Accept header
        format_type = parse_accept_header(accept)
        
        logger.info(f"Requested format: {format_type} (Accept: {accept})")

        if format_type == "json":
            # JSON response with base64 content and metadata
            content_b64 = base64.b64encode(content).decode("utf-8")
            
            json_response = {
                "success": True,
                "url": metadata["url"],
                "size": metadata["size"],
                "content_type": metadata["content_type"],
                "content": content_b64,
                "metadata": metadata,
            }
            
            return Response(
                content=json.dumps(json_response),
                media_type="application/json",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )
            
        elif format_type == "text":
            # Plain text response
            text_content = convert_content_to_text(content, metadata["content_type"])
            
            return Response(
                content=text_content,
                media_type="text/plain; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )
            
        elif format_type == "markdown":
            # Markdown response  
            markdown_content = convert_content_to_markdown(content, metadata["content_type"])
            
            return Response(
                content=markdown_content,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )
            
        elif format_type == "html":
            # HTML response (if original was HTML, return as-is; otherwise decode)
            if 'html' in metadata["content_type"].lower():
                html_content = content
                response_content_type = metadata["content_type"]
            else:
                html_content = content.decode('utf-8', errors='ignore')
                response_content_type = "text/html; charset=utf-8"
            
            return Response(
                content=html_content,
                media_type=response_content_type,
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )
            
        else:
            # Raw response (default)
            return Response(
                content=content,
                media_type=metadata.get("content_type", "application/octet-stream"),
                headers={
                    "X-Original-URL": metadata["url"],
                    "Content-Length": str(metadata["size"]),
                },
            )

    except URLValidationError as e:
        logger.warning(f"URL validation failed for {url}: {e}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=str(e), error_type="validation_error"
            ).model_dump(),
        )

    except HTTPTimeoutError as e:
        logger.error(f"Timeout error for {url}: {e}")
        raise HTTPException(
            status_code=408,
            detail=ErrorResponse(error=str(e), error_type="timeout_error").model_dump(),
        )

    except HTTPClientError as e:
        logger.error(f"HTTP client error for {url}: {e}")
        # Map common HTTP errors to appropriate status codes
        if "404" in str(e):
            status_code = 404
        elif "403" in str(e):
            status_code = 403
        elif "401" in str(e):
            status_code = 401
        elif "500" in str(e):
            status_code = 502  # Bad Gateway
        else:
            status_code = 502  # Bad Gateway for other HTTP errors

        raise HTTPException(
            status_code=status_code,
            detail=ErrorResponse(error=str(e), error_type="http_error").model_dump(),
        )

    except DownloadError as e:
        logger.error(f"Download error for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=str(e), error_type="download_error"
            ).model_dump(),
        )

    except Exception as e:
        logger.exception(f"Unexpected error downloading {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Internal server error", error_type="internal_error"
            ).model_dump(),
        )