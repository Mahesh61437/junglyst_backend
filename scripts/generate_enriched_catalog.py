import json
import os
import sys
import re
import requests
import time
from bs4 import BeautifulSoup

# Add the parent directory to sys.path so we can import category_utils and plant_defaults
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
junglyst_root = os.path.dirname(backend_dir)
if junglyst_root not in sys.path:
    sys.path.insert(0, junglyst_root)

try:
    from category_utils import CATEGORY_MAP, TAG_TO_CATEGORY, pick_category_names, pick_tags
except ImportError:
    print("Warning: Could not import category_utils from junglyst root.")
    CATEGORY_MAP = {}
    TAG_TO_CATEGORY = {}
    def pick_category_names(names): return []
    def pick_tags(tags): return []

try:
    from plant_defaults import apply_plant_defaults
except ImportError:
    print("Warning: Could not import plant_defaults from junglyst root.")
    def apply_plant_defaults(p, n): return p


def clean_html(raw_html):
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    return soup.get_text(separator="\n").strip()


def parse_html_details(html_content):
    details = {
        "ph_range": "",
        "water_temperature": "",
        "care_level": "",
        "light_requirements": "",
        "co2_requirement": "",
        "growth_rate": ""
    }
    if not html_content:
        return details
        
    soup = BeautifulSoup(html_content, "html.parser")
    text = soup.get_text(separator=" ", strip=True)
    
    # pH Range
    ph_match = re.search(r'pH Range:?\s*([0-9.]+\s*-\s*[0-9.]+)', text, re.IGNORECASE)
    if ph_match:
        details["ph_range"] = ph_match.group(1).strip()
        
    # Temperature
    temp_match = re.search(r'Temperature:?\s*[^0-9]*([0-9]{2}[°]?[FfC]?\s*(?:to|-)\s*[0-9]{2}[°]?[FfC]?)', text, re.IGNORECASE)
    if temp_match:
        details["water_temperature"] = temp_match.group(1).strip()
        
    # Care level / Hardiness
    care_match = re.search(r'Hardiness:?\s*([^.]*)', text, re.IGNORECASE)
    if care_match:
        hardiness = care_match.group(1).lower()
        if "hardiest" in hardiness or "easy" in hardiness:
            details["care_level"] = "Easy"
        elif "moderate" in hardiness:
            details["care_level"] = "Medium"
        elif "difficult" in hardiness or "expert" in hardiness:
            details["care_level"] = "Advanced"

    # Light
    light_match = re.search(r'Light(?:ing| Intensity)?:?\s*([^.]*)', text, re.IGNORECASE)
    if light_match:
        l_text = light_match.group(1).lower()
        if "low" in l_text:
            details["light_requirements"] = "Low"
        elif "medium" in l_text or "moderate" in l_text:
            details["light_requirements"] = "Medium"
        elif "high" in l_text or "intense" in l_text:
            details["light_requirements"] = "High"
            
    # CO2
    co2_match = re.search(r'CO2:?\s*([^.]*)', text, re.IGNORECASE)
    if co2_match:
        c_text = co2_match.group(1).lower()
        if "not required" in c_text or "low" in c_text:
            details["co2_requirement"] = "Low"
        elif "high" in c_text or "required" in c_text:
            details["co2_requirement"] = "High"
        elif "medium" in c_text or "recommended" in c_text:
            details["co2_requirement"] = "Medium"
            
    # Growth Rate
    growth_match = re.search(r'Growth Rate:?\s*([^.]*)', text, re.IGNORECASE)
    if growth_match:
        g_text = growth_match.group(1).lower()
        if "slow" in g_text:
            details["growth_rate"] = "Slow"
        elif "fast" in g_text:
            details["growth_rate"] = "Fast"
        elif "moderate" in g_text or "medium" in g_text:
            details["growth_rate"] = "Medium"
            
    return details


def fetch_from_wikipedia(plant_name):
    print(f"    Fetching Wikipedia for '{plant_name}'...")
    try:
        # Simplistic search to get a title
        search_url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={requests.utils.quote(plant_name)}&utf8=&format=json"
        res = requests.get(search_url, timeout=5)
        search_data = res.json()
        if not search_data.get("query", {}).get("search"):
            return None, None
            
        title = search_data["query"]["search"][0]["title"]
        
        # Fetch summary extract
        summary_url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro&explaintext&titles={requests.utils.quote(title)}&format=json"
        sum_res = requests.get(summary_url, timeout=5)
        pages = sum_res.json().get("query", {}).get("pages", {})
        extract = ""
        for p_id, p_info in pages.items():
            if "extract" in p_info:
                extract = p_info["extract"]
                break
                
        # Simple extraction logic for origin (Africa, Asia, America, etc.)
        origin = ""
        origin_match = re.search(r'native to ([^.]*)', extract, re.IGNORECASE)
        if origin_match:
            origin = origin_match.group(1).strip()
            
        return extract.strip(), origin.strip()
    except Exception as e:
        print(f"    Wiki error for {plant_name}: {e}")
        return None, None


def main():
    api_url = "http://api.aquaticexotica.com/api/products/?page=1"
    all_products = []
    
    print("Starting enriched catalog generation...")
    while api_url:
        print(f"Fetching {api_url} ...")
        resp = requests.get(api_url)
        resp.raise_for_status()
        data = resp.json()
        
        results = data.get("results", [])
        for p in results:
            name = p.get("name", "")
            print(f"  -> Processing: {name}")
            
            raw_html = p.get("description", "")
            clean_desc = clean_html(raw_html)
            
            # Extract basic details from HTML
            parsed_details = parse_html_details(raw_html)
            
            # Resolve tags and categories
            raw_tags = [t.get("name") for t in p.get("tagDetails", []) if t.get("name")]
            raw_cats = [c.get("name") for c in p.get("categories", []) if c.get("name")]
            
            tags = pick_tags(raw_tags)
            cats = pick_category_names(raw_cats)
            
            # Multi-category deduction
            names_set = set(raw_cats)
            mapped_subs = []
            
            # Aquatic Mosses
            if "Moss" in names_set and "Aquatic Plants" in names_set:
                mapped_subs.append(("Plants", "Aquatic Moss"))
                mapped_subs.append(("Terrarium & Paludarium", "Terrarium Moss"))
                mapped_subs.append(("Plants", "Aquatic Plants"))
            # Terrarium Mosses
            elif "Moss" in names_set and "Terrarium Plants" in names_set:
                mapped_subs.append(("Terrarium & Paludarium", "Terrarium Moss"))
                mapped_subs.append(("Plants", "Terrarium Plants"))
            # Rare/Exotic Rhizomes
            elif "Rhizome plants" in names_set and "Premium" in names_set:
                mapped_subs.append(("Plants", "Rare & Exotic"))
                mapped_subs.append(("Plants", "Aquatic Plants"))
                mapped_subs.append(("Terrarium & Paludarium", "Terrarium Plants"))
            # Standard Rhizomes
            elif "Rhizome plants" in names_set:
                mapped_subs.append(("Plants", "Aquatic Plants"))
                mapped_subs.append(("Terrarium & Paludarium", "Terrarium Plants"))
            # Exotic/Indoor/Terrarium
            elif "Terrarium Plants" in names_set or "Indoor Plants" in names_set or "Exotic Plants" in names_set:
                mapped_subs.append(("Terrarium & Paludarium", "Terrarium Plants"))
                
            # Deduplicate
            mapped_subs = list(dict.fromkeys(mapped_subs))
            
            # Fallback
            if not mapped_subs:
                for cat_name in raw_cats:
                    if cat_name in CATEGORY_MAP:
                        mapped_subs.append(CATEGORY_MAP[cat_name])
            
            # Construct product dictionary
            product_dict = {
                "source_id": p.get("id"),
                "name": name,
                "price": str(p.get("price")),
                "stock": p.get("stock"),
                "categories": [c[0] for c in mapped_subs] if mapped_subs else [],
                "sub_categories": [c[1] for c in mapped_subs] if mapped_subs else [],
                "tags": tags,
                "ph_range": parsed_details["ph_range"],
                "water_temperature": parsed_details["water_temperature"],
                "care_level": parsed_details["care_level"],
                "light_requirements": parsed_details["light_requirements"],
                "co2_requirement": parsed_details["co2_requirement"],
                "growth_rate": parsed_details["growth_rate"],
                "origin": "",
                "scientific_name": "",
                "description": clean_desc,
                "images": [p.get("imageUrl")] + [img.get("image_url") for img in p.get("images", []) if img.get("image_url")]
            }
            
            # Apply plant_defaults
            product_dict = apply_plant_defaults(product_dict, name)
            
            # Fallback to Wikipedia for missing essential fields
            if not product_dict.get("origin") or len(product_dict.get("description", "")) < 50:
                # Strip out common ecommerce terms for better wiki searching
                search_name = name.lower().replace("1 pot", "").replace("tissue culture", "").replace("tc", "").replace("()", "").strip()
                wiki_desc, wiki_orig = fetch_from_wikipedia(search_name)
                if wiki_orig and not product_dict.get("origin"):
                    product_dict["origin"] = wiki_orig
                if wiki_desc and len(product_dict.get("description", "")) < 50:
                    product_dict["description"] = wiki_desc
                    
            all_products.append(product_dict)
            time.sleep(0.5)  # Be polite to APIs
            
        api_url = data.get("next")
        
    output_file = "aquatic_exotica_enriched.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_products, f, indent=2, ensure_ascii=False)
        
    print(f"\nSuccessfully enriched and saved {len(all_products)} products to {output_file}")


if __name__ == "__main__":
    main()
