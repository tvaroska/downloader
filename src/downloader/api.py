"""API endpoints for the downloader service."""

import asyncio
import base64
import logging
import re
import json
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Response, Path, Header, Request, Depends
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from bs4 import BeautifulSoup

from .validation import validate_url, URLValidationError
from .http_client import get_client, HTTPClientError, HTTPTimeoutError, DownloadError
from .pdf_generator import generate_pdf_from_url, PDFGeneratorError, get_pdf_generator, get_shared_pdf_generator
from .auth import get_api_key, security

logger = logging.getLogger(__name__)

router = APIRouter()

# Concurrency control for PDF generation
PDF_SEMAPHORE = asyncio.Semaphore(5)  # Max 5 concurrent PDF generations


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
        Format string: 'text', 'html', 'markdown', 'pdf', 'json', or 'raw'
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
    elif "application/pdf" in accept_header:
        return "pdf"
    elif "application/json" in accept_header:
        return "json"
    else:
        return "raw"


async def convert_content_to_text_with_playwright_fallback(url: str) -> str:
    """
    Convert content to plain text using Playwright to get rendered HTML.
    This is used as a fallback when BeautifulSoup returns empty text.
    
    Args:
        url: The URL to fetch and convert to text
        
    Returns:
        Plain text representation with article content extracted
        
    Raises:
        Exception: If Playwright conversion fails
    """
    try:
        logger.info(f"🔄 Starting Playwright text fallback for {url}")
        generator = get_shared_pdf_generator()
        if not generator or not generator.pool:
            raise Exception("PDF generator pool not initialized")
                
        browser = await generator.pool.get_browser()
        context = None
        page = None
            
        try:
                # Create isolated context for this request
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1280, 'height': 720},
                    ignore_https_errors=False,
                    java_script_enabled=True,
                    bypass_csp=False
                )
                
                # Create page in isolated context
                page = await context.new_page()
                
                logger.info(f"🌐 Loading page with Playwright: {url}")
                # Navigate to page with timeout
                response = await page.goto(
                    url, 
                    wait_until='networkidle',
                    timeout=30000
                )
                
                if not response or response.status >= 400:
                    raise Exception(f"Failed to load page: {url}")
                
                logger.debug(f"Page loaded, waiting for network idle: {url}")
                # Wait for page to be fully loaded
                await page.wait_for_load_state('networkidle', timeout=30000)
                
                logger.debug(f"Extracting rendered HTML content for text: {url}")
                # Get the rendered HTML content
                html_content = await page.content()
                
                logger.info(f"📄 Processing rendered HTML content ({len(html_content)} chars) for text extraction: {url}")
                # Convert HTML to text using BeautifulSoup
                soup = BeautifulSoup(html_content, 'lxml')
                
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
                
        finally:
            # Cleanup in reverse order
            if context:
                await context.close()
            if generator.pool:
                await generator.pool.release_browser(browser)
                    
    except Exception as e:
        logger.error(f"Playwright fallback failed for {url}: {e}")
        raise


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


async def convert_content_to_markdown_with_playwright_fallback(url: str) -> str:
    """
    Convert content to markdown using Playwright to get rendered HTML.
    This is used as a fallback when BeautifulSoup returns empty markdown.
    
    Args:
        url: The URL to fetch and convert to markdown
        
    Returns:
        Markdown representation with article content extracted
        
    Raises:
        Exception: If Playwright conversion fails
    """
    try:
        logger.info(f"🔄 Starting Playwright markdown fallback for {url}")
        generator = get_shared_pdf_generator()
        if not generator or not generator.pool:
            raise Exception("PDF generator pool not initialized")
                
        browser = await generator.pool.get_browser()
        context = None
        page = None
            
        try:
                # Create isolated context for this request
                context = await browser.new_context(
                    user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1280, 'height': 720},
                    ignore_https_errors=False,
                    java_script_enabled=True,
                    bypass_csp=False
                )
                
                # Create page in isolated context
                page = await context.new_page()
                
                logger.info(f"🌐 Loading page with Playwright: {url}")
                # Navigate to page with timeout
                response = await page.goto(
                    url, 
                    wait_until='networkidle',
                    timeout=30000
                )
                
                if not response or response.status >= 400:
                    raise Exception(f"Failed to load page: {url}")
                
                logger.debug(f"Page loaded, waiting for network idle: {url}")
                # Wait for page to be fully loaded
                await page.wait_for_load_state('networkidle', timeout=30000)
                
                logger.debug(f"Extracting rendered HTML content for markdown: {url}")
                # Get the rendered HTML content
                html_content = await page.content()
                
                logger.info(f"📝 Processing rendered HTML content ({len(html_content)} chars) for markdown extraction: {url}")
                # Convert HTML to markdown using BeautifulSoup
                soup = BeautifulSoup(html_content, 'lxml')
                
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
                
        finally:
            # Cleanup in reverse order
            if context:
                await context.close()
            if generator.pool:
                await generator.pool.release_browser(browser)
                    
    except Exception as e:
        logger.error(f"Playwright markdown fallback failed for {url}: {e}")
        raise


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
    api_key: Optional[str] = Depends(get_api_key),
) -> Response:
    """
    Download content from a single URL with content negotiation.

    Args:
        url: The URL to download from
        accept: Accept header for content negotiation
        request: FastAPI request object
        api_key: API key for authentication (if DOWNLOADER_KEY env var is set)

    Returns:
        Response with content in requested format

    Raises:
        HTTPException: For various error conditions including authentication failures
        
    Authentication (if DOWNLOADER_KEY environment variable is set):
    - Authorization: Bearer <api_key>
    - X-API-Key: <api_key>
        
    Content Negotiation via Accept Header:
    - text/plain: Returns plain text (HTML tags stripped)
    - text/html: Returns original HTML content
    - text/markdown: Returns markdown conversion of HTML
    - application/pdf: Returns PDF generated via Playwright with JavaScript rendering
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
            
            # Log BS4 extraction results for HTML content
            if 'html' in metadata["content_type"].lower():
                logger.info(f"BS4 text extraction for {validated_url}: {len(text_content)} characters extracted")
                
                # Check if BeautifulSoup returned empty text for HTML content and use Playwright fallback
                if not text_content.strip():
                    logger.warning(f"BS4 returned empty text for HTML content at {validated_url} (content size: {len(content)} bytes)")
                    logger.info(f"Triggering Playwright fallback for {validated_url}")
                    try:
                        fallback_start_time = asyncio.get_event_loop().time()
                        text_content = await convert_content_to_text_with_playwright_fallback(validated_url)
                        fallback_duration = asyncio.get_event_loop().time() - fallback_start_time
                        logger.info(f"✅ Playwright fallback successful for {validated_url}: {len(text_content)} characters extracted in {fallback_duration:.2f}s")
                    except Exception as e:
                        logger.error(f"❌ Playwright fallback failed for {validated_url}: {str(e)}")
                        logger.info(f"Falling back to original empty text for {validated_url}")
                        # Keep the original empty text if Playwright fails
                else:
                    logger.debug(f"BS4 successfully extracted content for {validated_url}, no fallback needed")
            else:
                logger.debug(f"Non-HTML content for {validated_url}, skipping fallback logic")
            
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
            
            # Log BS4 extraction results for HTML content
            if 'html' in metadata["content_type"].lower():
                logger.info(f"BS4 markdown extraction for {validated_url}: {len(markdown_content)} characters extracted")
                
                # Check if BeautifulSoup returned empty markdown for HTML content and use Playwright fallback
                if not markdown_content.strip():
                    logger.warning(f"BS4 returned empty markdown for HTML content at {validated_url} (content size: {len(content)} bytes)")
                    logger.info(f"Triggering Playwright markdown fallback for {validated_url}")
                    try:
                        fallback_start_time = asyncio.get_event_loop().time()
                        markdown_content = await convert_content_to_markdown_with_playwright_fallback(validated_url)
                        fallback_duration = asyncio.get_event_loop().time() - fallback_start_time
                        logger.info(f"✅ Playwright markdown fallback successful for {validated_url}: {len(markdown_content)} characters extracted in {fallback_duration:.2f}s")
                    except Exception as e:
                        logger.error(f"❌ Playwright markdown fallback failed for {validated_url}: {str(e)}")
                        logger.info(f"Falling back to original empty markdown for {validated_url}")
                        # Keep the original empty markdown if Playwright fails
                else:
                    logger.debug(f"BS4 successfully extracted markdown for {validated_url}, no fallback needed")
            else:
                logger.debug(f"Non-HTML content for {validated_url}, skipping markdown fallback logic")
            
            return Response(
                content=markdown_content,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )
            
        elif format_type == "pdf":
            # PDF response - generate PDF using Playwright with concurrency control
            logger.info(f"Generating PDF for: {validated_url}")
            
            # Check if PDF service is available
            if PDF_SEMAPHORE.locked():
                logger.warning(f"PDF service at capacity, rejecting request for: {validated_url}")
                raise HTTPException(
                    status_code=503,
                    detail=ErrorResponse(
                        error="PDF service temporarily unavailable. Please try again later.",
                        error_type="service_unavailable"
                    ).model_dump(),
                )
            
            # Generate PDF with concurrency control
            async with PDF_SEMAPHORE:
                pdf_content = await generate_pdf_from_url(validated_url)
            
            return Response(
                content=pdf_content,
                media_type="application/pdf",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "Content-Length": str(len(pdf_content)),
                    "Content-Disposition": "inline; filename=\"download.pdf\"",
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

    except PDFGeneratorError as e:
        logger.error(f"PDF generation failed for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=f"PDF generation failed: {e}", 
                error_type="pdf_generation_error"
            ).model_dump(),
        )

    except HTTPException:
        # Re-raise HTTPExceptions (like our 503 error) without modification
        raise
        
    except Exception as e:
        logger.exception(f"Unexpected error downloading {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Internal server error", error_type="internal_error"
            ).model_dump(),
        )