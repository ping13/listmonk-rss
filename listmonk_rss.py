import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import feedparser
from jinja2 import Template
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import click
import logging

logging.basicConfig(level=logging.INFO)  # Set to DEBUG, INFO, WARNING, ERROR, or CRITICAL

# Load environment variables
load_dotenv()

# Constants
TEMPLATE_FILE = Path("template.md.j2")

def get_opengraph_data(url):
    response = httpx.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    og_data = {}
    for meta in soup.find_all('meta'):
        prop = meta.get('property', '')
        if prop.startswith('og:'):
            key = prop[3:]
            og_data[key] = meta.get('content', '')
    return og_data


def get_last_update() -> datetime:
    """Get the last update timestamp from GitHub repo variable."""
    github_token = os.getenv("GH_TOKEN")
    repo = os.getenv("GH_REPOSITORY")
    url = f"https://api.github.com/repos/{repo}/actions/variables/LAST_UPDATE"
    
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    try:
        response = httpx.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return datetime.fromisoformat(data["value"])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return datetime.min
        raise


def save_last_update(timestamp: datetime):
    """Save the last update timestamp to GitHub repo variable."""
    github_token = os.getenv("GH_TOKEN")
    repo = os.getenv("GH_REPOSITORY")
    url = f"https://api.github.com/repos/{repo}/actions/variables/LAST_UPDATE"
    
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    data = {
        "name": "LAST_UPDATE",
        "value": timestamp.isoformat()
    }
    
    response = httpx.patch(url, headers=headers, json=data)
    response.raise_for_status()
    logging.info(f"Saved last update timestamp to GitHub repo variable")


def fetch_rss_feed(feed_url: str, last_update: datetime) -> list:
    """Fetch and parse RSS feed, returning new items since last update."""
    feed = feedparser.parse(feed_url)
    new_items = []
    logging.info(f"There are in total {len(feed.entries)} entries for {feed_url}")
    for entry in feed.entries:        
        if datetime(*entry.published_parsed[:6]) > last_update:
            og = get_opengraph_data(entry.link)
            if og.get("image"):
                entry.media_content=og.get("image")
            new_items.append(entry)

    return new_items


def get_list_id(host: str, api_user: str, api_token: str, list_name: str) -> int:
    """Get list ID from list name using Listmonk API."""
    url = f"{host}/api/lists"
    auth=(api_user, api_token)
    headers = {
        "Content-Type": "application/json"
    }
    
    with httpx.Client() as client:
        response = client.get(url, headers=headers, auth=auth )
        response.raise_for_status()
        
        # Find the list with matching name
        lists = response.json()["data"]["results"]
        for lst in lists:
            if lst["name"] == list_name:
                return lst["id"]
        
        raise ValueError(f"List '{list_name}' not found")


def create_campaign_content(items: list, template: Template) -> str:
    """Generate campaign content using Jinja2 template."""
    return template.render(items=items)


def schedule_campaign(host: str, api_user: str, api_token: str, list_id: int, content: str, subject: str, dry_run: bool = False):
    """Send campaign draft using Listmonk API."""
    url = f"{host}/api/campaigns"
    auth=(api_user, api_token)
    headers = {
        "Content-Type": "application/json"
    }
    current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M")

    # for the send time, we assume that the linkmonk server runs in UTC (this
    # is what the default in PikaPods are)
    if dry_run:
        # Set delay to 10 years for dry run
        print("*** This is a dry run, the campaign is scheduled 10 years in the future")
        delay_mins = 365 * 24 * 60 * 10
    else:
        delay_mins = int(os.getenv("DELAY_SEND_MINS", 30))
        
    send_datetime = datetime.now(timezone.utc) + timedelta(minutes=delay_mins)
    send_datetime = send_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "name" : f"RSS Update Newsletter, {current_datetime}",
        "subject": subject,
        "lists": [list_id],
        "body": content,
        "content_type": "markdown",
        "type": "regular",
        "send_at" : send_datetime
    }
    
    with httpx.Client() as client:
        response = client.post(url, json=data, headers=headers,auth=auth)
        response.raise_for_status()

        parsed = response.json()
        assert parsed.get("data",{}).get("id",None), "Cannot get the id of the created campaign"
        campaign_id = parsed.get("data",{}).get("id")

        print(f"Campaign draft {campaign_id} successfully created!")

        
    url = f"{url}/{campaign_id}/status"
    data = {"status": "scheduled"}
    headers = {
        "Content-Type": "application/json"
    }
    
    with httpx.Client() as client:
        response = client.put(url, json=data, auth=auth)
        response.raise_for_status()

        assert parsed.get("data",{}).get("id",None) == campaign_id, f"Cannot schedule campaign {campaign_id}"

        print(f"Campaign {campaign_id} successfully scheduled with {delay_mins} mins delay!")

        # Send Pushover notification
        pushover_user_key = os.getenv("PUSHOVER_USER_KEY")
        pushover_api_token = os.getenv("PUSHOVER_API_TOKEN")

        if pushover_user_key and pushover_api_token:
            response = httpx.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": pushover_api_token,
                    "user": pushover_user_key,
                    "message": f"A new campaign has been successfully scheduled with {delay_mins} mins delay! Check if you want to review this before sending.",
                    "title": "Newsletter for your blog"
                },
                headers={"Content-type": "application/x-www-form-urlencoded"}
            )
            response.raise_for_status()

    return True

@click.command()
@click.option("--dry-run", is_flag=True, help="Create draft campaign with a 10-year delay and don't update last update time.")
def main(dry_run: bool):
    if dry_run:
        print("*** This is a dry run")


    assert os.getenv("RSS_FEED"), "No RSS feed given"
    # Load template
    template = Template(TEMPLATE_FILE.read_text())
    
    # Get last update time
    last_update = get_last_update()
    
    # Fetch new RSS items
    items = list(reversed(fetch_rss_feed(os.getenv("RSS_FEED"), last_update)))
    
    if not items:
        print(f"No new items found, I keep the update as of my last state '{last_update}' (UTC) in GitHub.")
        return
    
    # Create campaign content
    content = create_campaign_content(items, template)
        
    # Get list ID
    list_id = get_list_id(
        host=os.getenv("LISTMONK_HOST"),
        api_user=os.getenv("LISTMONK_API_USER"),
        api_token=os.getenv("LISTMONK_API_TOKEN"),
        list_name=os.getenv("LIST_NAME")
    )
    
    # Schedule campaign 
    success = schedule_campaign(
        host=os.getenv("LISTMONK_HOST"),
        api_user=os.getenv("LISTMONK_API_USER"),
        api_token=os.getenv("LISTMONK_API_TOKEN"),
        list_id=list_id,
        content=content,
        subject=os.getenv("LIST_NAME"),
        dry_run=dry_run
    )
    
    # Update last update time only if not dry run and successful
    if success and not dry_run:
        save_last_update(datetime.now())
    elif dry_run:
        print("*** This is a dry run, I don't update the last_save state")
    elif not success:
        print("*** Something went wrong with scheduling the campaign")


if __name__ == "__main__":
    main()
