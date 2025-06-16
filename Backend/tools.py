import requests
import base64
import mimetypes
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import os
from typing import List, Dict, Union
import xml.etree.ElementTree as ET
import re
from collections import defaultdict

class PixabayImageSearchTool:
    """
    A tool for AI Agents to search and retrieve image links from Pixabay.
    It wraps the image search functionality as a LongRunningFunctionTool for ADK.
    """
    def __init__(self):
        """
        Initializes the Pixabay Image Search Tool with your Pixabay API key.

        Args:
            api_key: Your personal Pixabay API key. It is strongly
                     recommended to load this from an environment variable
                     (e.g., `os.getenv("PIXABAY_API_KEY")`) or a secure
                     configuration system, rather than hardcoding it.
        """
        api_key = os.getenv("PIXABAY_API_KEY")

        if not api_key:
            raise ValueError("Pixabay API key cannot be empty. Please provide a valid key.")
        self.api_key = api_key
        # Wrap the internal search function using ADK's LongRunningFunctionTool
        self.tool = self._search_images_internal
        # Propagate docstring and type hints from the internal function to the tool object
        self.__doc__ = self.tool.__doc__
        self.__annotations__ = self.tool.__annotations__

    def _search_images_internal(
        self,
        queries_info: List[Dict[str, Union[str, int]]],
    ) -> Dict[str, List[str]]:
        """
        Searches for images on Pixabay based on a list of queries.

        Args:
            queries_info: A list of dictionaries, where each dictionary represents
                          a search request and must contain:
                - "query" (str): The search term (e.g., "yellow flowers").
                - "num_images" (int): The desired number of image links to retrieve.
                                      (Will fetch up to 500 images per query due to Pixabay API limits).
            tool_context: An optional context object provided by the ADK framework.

        Returns:
            A dictionary where:
            - Keys are the original search queries (str).
            - Values are lists of `webformatURL` image links (List[str]).

            If no images are found for a query, that query will not be included
            in the output dictionary. If fewer images are found than requested,
            all available images will be returned for that query.
        """
        base_url = "https://pixabay.com/api/"
        results: Dict[str, List[str]] = {}

        for item in queries_info:
            query = item.get("query")
            # Default to 1 image if 'num_images' is not specified or invalid
            num_images = int(item.get("num_images", 1)) 

            if not query:
                print(f"Warning: Skipping search item due to missing or empty 'query'. Item: {item}")
                continue
            if num_images <= 0:
                print(f"Warning: Skipping search item for query '{query}' as 'num_images' is not positive.")
                continue

            images_for_current_query: List[str] = []
            
            # Pixabay API limits `per_page` to 200 and `totalHits` (accessible images) to 500 per query.
            # We will fetch up to 500 images or the requested `num_images`, whichever is smaller.
            effective_num_images_to_fetch = min(num_images, 500)
            
            # Calculate the number of API pages (requests) needed, max 200 images per page
            # Using ceiling division to ensure we cover all images if not a multiple of 200.
            pages_to_fetch = (effective_num_images_to_fetch + 199) // 200 

            for page_num in range(1, pages_to_fetch + 1):
                # Stop if we've already collected enough images
                if len(images_for_current_query) >= effective_num_images_to_fetch:
                    break

                # Calculate how many images to request on the current page
                remaining_to_fetch = effective_num_images_to_fetch - len(images_for_current_query)
                current_per_page = min(200, remaining_to_fetch) # Max 200 per page

                if current_per_page <= 0: # Should not happen if logic is correct, but as a safeguard
                    break

                params = {
                    "key": self.api_key,
                    "q": query,
                    "image_type": "photo", # Filtering for photos as per common use case
                    "per_page": current_per_page,
                    "page": page_num,
                    "safesearch": "true" # Ensure family-friendly results by default
                }

                try:
                    response = requests.get(base_url, params=params)
                    response.raise_for_status()  # Raises HTTPError for 4xx/5xx responses
                    data = response.json()

                    if "hits" in data:
                        for hit in data["hits"]:
                            # The documentation suggests `webformatURL` for temporary display of search results.
                            if "webformatURL" in hit:
                                images_for_current_query.append(hit["webformatURL"])
                                # Stop once we have gathered the required number of images
                                if len(images_for_current_query) >= effective_num_images_to_fetch:
                                    break
                    else:
                        print(f"No 'hits' found in response for query '{query}' (Page {page_num}). Response: {data}")

                except requests.exceptions.HTTPError as e:
                    print(f"HTTP Error for query '{query}' (Page {page_num}): Status {e.response.status_code} - {e.response.text}")
                    # Common errors: 400 (Bad Request), 429 (Too Many Requests), 500 (Internal Server Error)
                    # For rate limits (429), the tool might need a retry mechanism with backoff.
                    break # Stop processing this query on HTTP error
                except requests.exceptions.RequestException as e:
                    print(f"Network or request error for query '{query}' (Page {page_num}): {e}")
                    break # Stop processing this query on network error
                except ValueError: # JSONDecodeError is a subclass of ValueError
                    print(f"Failed to decode JSON response for query '{query}' (Page {page_num}).")
                    break # Stop processing this query on invalid JSON
                except Exception as e:
                    print(f"An unexpected error occurred for query '{query}' (Page {page_num}): {e}")
                    break

            # Only add the query to results if images were found
            if images_for_current_query:
                results[query] = images_for_current_query

        return results


def fetch_image_as_base64(src):
    """Fetch an image from a URL or local path and return it as a base64 data URI."""
    try:
        if src.startswith("data:"):
            return src  # already base64

        if src.startswith("http"):
            response = requests.get(src, timeout=5)
            response.raise_for_status()
            content = response.content
            mime = response.headers.get("Content-Type", mimetypes.guess_type(src)[0])
        else:
            with open(src, 'rb') as f:
                content = f.read()
            mime = mimetypes.guess_type(src)[0] or 'application/octet-stream'

        encoded = base64.b64encode(content).decode('utf-8')
        return f"data:{mime};base64,{encoded}"
    except Exception as e:
        print(f"[!] Could not convert image {src}:\n {e}\n Using Dummy Image Instead")
        try:
            response = requests.get('https://dummyjson.com/image/400x200?type=png&text=failed+to+generate+image&fontSize=16', timeout=5)
            response.raise_for_status()
            content = response.content
            mime = response.headers.get("Content-Type", mimetypes.guess_type(src)[0])
            encoded = base64.b64encode(content).decode('utf-8')
            return f"data:{mime};base64,{encoded}"
        except:
            print(f"[!] Could not load dummy image {src}:\n {e}")
        return src



def replace_svg_image_links_with_base64(svg_content):
    """Replaces <image> tags' href or xlink:href in SVG content with base64 image data."""
    soup = BeautifulSoup(svg_content, 'lxml-xml')  # 'xml' parser preserves SVG structure
    image_tags = soup.find_all('image')

    for tag in image_tags:
        href = tag.get('xlink:href') or tag.get('href')
        if href:
            data_uri = fetch_image_as_base64(href)
            if tag.has_attr('xlink:href'):
                tag['xlink:href'] = data_uri
            else:
                tag['href'] = data_uri

    return str(soup)

def clean_svg(svg_text: str) -> str:
    """
    Cleans an SVG string by removing XML declarations and comments.

    Args:
        svg_text (str): The raw SVG XML string.

    Returns:
        str: Cleaned SVG content.
    """
    # Remove XML declaration
    svg_text = re.sub(r'<\?xml.*?\?>', '', svg_text, flags=re.DOTALL)

    # Remove all HTML/XML comments
    svg_text = re.sub(r'<!--.*?-->', '', svg_text, flags=re.DOTALL)

    # Optional: strip leading/trailing whitespace
    return svg_text.strip()


def get_material_icon_svg(icon_name: str, font_family: str, fill):
    """
    Downloads the SVG for a given Material Icon name from the Google Fonts CDN.

    Args:
        icon_name (str): The name of the Material Icon (e.g., "home", "search").

    Returns:
        str: The SVG content as a string, or None if the icon is not found or an error occurs.
    """
    retry = 0

    if not icon_name:
        return None

    if 'Material Icons' in font_family:
        url = f"https://fonts.gstatic.com/s/i/materialiconsround/{icon_name}/v1/24px.svg"

    else:
        if fill and fill == "true":
            url = f"https://fonts.gstatic.com/s/i/short-term/release/materialsymbolsrounded/{icon_name}/fill1/48px.svg"
        else:
            url = f"https://fonts.gstatic.com/s/i/short-term/release/materialsymbolsrounded/{icon_name}/default/48px.svg"

    while retry < 2:
        try:
            response = requests.get(url, timeout=100)
            response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
            return clean_svg(response.text)
        except requests.exceptions.RequestException as e:
            if 'Material Icons' in font_family:
                print(f"Warning: Could not fetch icon '{icon_name}', Retrying with Material Symbols x48. Error: {e}")
                url = f"https://fonts.gstatic.com/s/i/short-term/release/materialsymbolsrounded/{icon_name}/default/48px.svg"
            if 'Material Symbols' in font_family:
                print(f"Warning: Could not fetch icon '{icon_name}', Retrying with Material Symbols x24. Error: {e}")
                url = f"https://fonts.gstatic.com/s/i/short-term/release/materialsymbolsrounded/{icon_name}/default/24px.svg"
            retry += 1

    print(f"Could not fetch icon '{icon_name}'. Skipping Icon..")
    return None

def parse_css_styles(svg_root) -> dict:
    styles = defaultdict(dict)
    style_tags = svg_root.findall('.//{http://www.w3.org/2000/svg}style')
    for style_tag in style_tags:
        css_content = style_tag.text or ''
        rules = re.findall(r'\.([\w\-]+)\s*\{([^}]+)\}', css_content)
        for class_name, declarations in rules:
            for decl in declarations.split(';'):
                if ':' in decl:
                    prop, val = decl.split(':', 1)
                    styles[class_name.strip()][prop.strip()] = val.strip()
    return styles

def replace_material_icons_in_svg(svg_string: str) -> str:
    ET.register_namespace('', "http://www.w3.org/2000/svg")
    
    try:
        root = ET.fromstring(svg_string)
    except ET.ParseError as e:
        print(f"Error parsing SVG: {e}")
        return svg_string

    ns = {'svg': 'http://www.w3.org/2000/svg'}
    parent_map = {c: p for p in root.iter() for c in p}
    css_styles = parse_css_styles(root)
    replacements = []

    for text_element in root.findall('.//svg:text', ns):
        font_family = text_element.get('font-family', '')
        class_attr = text_element.get('class', '')
        classes = class_attr.split()

        for cls in classes:
            if not font_family and 'font-family' in css_styles.get(cls, {}):
                font_family = css_styles[cls]['font-family']

        if 'Material' in font_family:
            icon_name = text_element.text.strip() if text_element.text else ''
            print(f"Found Material Icon text: '{icon_name}'")

            selected = text_element.get('selected', False)
            icon_svg_text = get_material_icon_svg(icon_name, font_family, selected)
            if not icon_svg_text:
                print(f"  -> Skipping '{icon_name}' as its SVG could not be fetched.")
                continue

            try:
                icon_root = ET.fromstring(icon_svg_text)
                view_box = icon_root.get('viewBox')

                # Get all graphical children (e.g., paths, circles, groups, etc.)
                graphical_elements = [
                    elem for elem in icon_root if elem.tag.endswith(('path', 'g', 'circle', 'rect', 'polygon', 'polyline'))
                ]

                if not graphical_elements:
                    print(f"  -> No usable graphical content found in SVG for '{icon_name}'.")
                    continue

                fill_color = text_element.get('fill', 'black')
                font_size_str = text_element.get('font-size', '24')
                font_size = float(re.sub(r'[^\d.]', '', font_size_str))
                x = float(text_element.get('x', 0))
                y = float(text_element.get('y', 0))
                text_anchor = text_element.get('text-anchor', 'start')

                new_y = y - (font_size / 2) - 6
                if text_anchor == 'middle':
                    new_x = x - (font_size / 2)
                elif text_anchor == 'end':
                    new_x = x - font_size
                else:
                    new_x = x

                # Create wrapper <svg> for the entire icon
                new_icon_svg = ET.Element('svg', {
                    'x': str(new_x),
                    'y': str(new_y),
                    'width': str(font_size),
                    'height': str(font_size),
                    'viewBox': view_box,
                    'fill': fill_color
                })

                for elem in graphical_elements:
                    new_icon_svg.append(elem)

                parent = parent_map.get(text_element)
                if parent is not None:
                    replacements.append((parent, text_element, new_icon_svg))

            except Exception as e:
                print(f"  -> Error processing icon '{icon_name}': {e}")

    for parent, old_element, new_element in replacements:
        index = list(parent).index(old_element)
        parent.remove(old_element)
        parent.insert(index, new_element)
        print(f"Replaced text '{old_element.text.strip()}' with complex SVG icon.")

    return ET.tostring(root, encoding='unicode', method='xml')