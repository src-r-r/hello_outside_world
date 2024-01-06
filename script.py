"""
An example of extension. It does nothing, but you can add transformations
before the return statements to customize the webui behavior.

Starting from history_modifier and ending in output_modifier, the
functions are declared in the same order that they are called at
generation time.
"""

import typing as T

import gradio as gr
import torch
from transformers import LogitsProcessor
import nltk
import re

from modules import chat, shared
from modules.text_generation import (
    decode,
    encode,
    generate_reply,
)
import json
from pathlib import Path

from newspaper import Article, fulltext, Config as NewsConfig

from typing import NamedTuple


class UrlContent(NamedTuple):
    url: str
    is_summary: bool
    article: Article


class OutputFooter(NamedTuple):
    url: T.AnyStr
    user_description: T.Optional[T.AnyStr]
    title: T.AnyStr
    summary: T.AnyStr


FETCH_ERROR = "__error__"

HERE = Path(__file__).resolve().parent
TEXTGEN_ROOT = HERE.parent.parent
CHARACTERS = TEXTGEN_ROOT / "characters"
MODELS = TEXTGEN_ROOT / "models"

assert CHARACTERS.exists(), f"Directory {CHARACTERS} does not exist."
assert MODELS.exists(), f"Directory {MODELS} does not exist."

PARAMS_PATH = TEXTGEN_ROOT / "hello_outside_world_settings.json"

USE_FOR_CHOICES = ["Chats", "Notebook &amp; Default", "Both"]

# markdown URLs.
RX_MD_URLS = re.compile(r"(?P<description>[^()]+)\((?P<url>[^)]+)\)").finditer

# Not relaly RFC 3986, but who's counting?
RX_RFC_3986 = re.compile(
    r"\w{2,6}://"  # Scheme
    r"[^\s]+"  # Anything but a space
)


def load_params_from_file():
    if PARAMS_PATH.exists():
        return json.loads(PARAMS_PATH.read_text())
    return None

params = load_params_from_file() or {
    "summary_length_trigger": 128,
    "max_summarization_length": 2048,
    "use_for": "Both",
    "enable_visible": True,
}


def load_params(*args, **kwargs):
    global params
    params.update(load_params_from_file())
    print("updated params")
    print(params)

def save_params():
    PARAMS_PATH.write_text(json.dumps(params))


def clean_url(url: T.AnyStr):
    tosub = re.compile("[^a-zA-Z0-9/]$", re.M)
    return tosub.sub("", url)


def extract_urls(text: T.AnyStr):
    urls = RX_RFC_3986.findall(text)
    # Clean any extraneous symbols from the end
    return [clean_url(url) for url in urls]

CONFIG = NewsConfig()

CONFIG.request_timeout = 300
CONFIG.fetch_images = False
CONFIG.thread_timeout_seconds = 3

def get_article_from_url(url, min_length=100, max_length=1024, prefer_articles=True):
    article = Article(url, config=CONFIG)
    article.download()
    article.parse()
    print(f"Fetched {article.title}")

    content = article.text

    if max_length < 0 or len(content) < max_length:
        print("No summary needed")
        return (False, article)

    print("Summarizing...")
    article.nlp()
    return (True, article)


def get_articles_from_input_message(
    input_message,
    min_length=40,
    total_max_length=1024,
    remaining_buffer=0.3,
    prefer_articles=True,
):
    max_length = int(total_max_length - (total_max_length * remaining_buffer))
    articles = {}
    for url in extract_urls(input_message):
        if max_length <= 0:
            raise RuntimeError(
                f"The length of the articles exceeded the maximum total length allowed ({total_max_length}). Either limit the number of articles or increase the maximum length."
            )
        try:
            is_summary, article = get_article_from_url(
                url,
                min_length=min_length,
                max_length=max_length,
                prefer_articles=prefer_articles,
            )
            articles[url] = UrlContent(url=url, is_summary=is_summary, article=article)
            if is_summary:
                max_length = total_max_length - len(article.summary)
            else:
                max_length = total_max_length - len(article.text)
        except Exception as e:
            print(f"ERROR: {e}")
            articles[url] = FETCH_ERROR
    return articles


def normalize_links_and_add_footer(
    input_message, summaries: T.Dict[T.AnyStr, UrlContent]
):
    """Make everythign markdown-y in the message.

    This function does a few things:
    1. Finds all already-existing markdown links.
    2. Find bare links not in markdown format.
    3. Replace all the non-markdown links with markdown ones.
    4. Add a "link footer" to the end of the message.
    """
    footer_summaries = {}
    out_message = input_message
    for match in RX_MD_URLS(input_message):
        groups = match.groupdict()
        url = groups["url"]
        if url not in summaries:
            # This was probably a false positive.
            continue
        if summaries[url] == FETCH_ERROR:
            continue
        footer_summaries[url] = OutputFooter(
            url=url,
            user_description=groups["description"],
            title=summaries[url].article.title,
            summary=summaries[url].article.summary
            if summaries[url].is_summary
            else summaries[url].text,
        )
    # Now go through and collect any bare urls (determined by summaries keys)
    for url, summary in summaries.items():
        if summary == FETCH_ERROR:
            continue
        footer_summaries[url] = OutputFooter(
            url=url,
            user_description=None,
            title=summary.article.title,
            summary=summary.article.summary
            if summary.is_summary
            else summary.article.text,
        )
    # Replace the bare URLs with markdown-formatted ones.
    for url, fs in footer_summaries.items():
        markdown = f"[{fs.title}]({url})"
        out_message = out_message.replace(url, markdown)
    
    # Mark any bad URLs as red.
    for url, summary in [(u, s) for (u, s) in summaries.items() if s == FETCH_ERROR]:
        out_message = out_message.replace(url, f"<span style='color: red'>{url}</span>")

    # Finaly, append the article summaries at the end:
    for url, fs in footer_summaries.items():
        title = fs.user_description or fs.title
        quoted_summary = "\n".join([f"> {s}" for s in fs.summary.split("\n")])
        out_message = out_message + f"\n\n> # {title}\n{quoted_summary}"

    return out_message


def chat_input_modifier(text, visible_text, state):
    """
    Modifies the user input string in chat mode (visible_text).
    You can also modify the internal representation of the user
    input (text) to change how it will appear in the prompt.
    """
    if params["use_for"] not in ("Chats", "Both"):
        return text, visible_text
    articles = get_articles_from_input_message(
        text,
        total_max_length=params["max_summarization_length"],
    )
    altered = normalize_links_and_add_footer(text, articles)
    if params["enable_visible"]:
        return altered, altered
    return altered, visible_text


def input_modifier(string, state, is_chat=False):
    """
    In default/notebook modes, modifies the whole prompt.

    In chat mode, it is the same as chat_input_modifier but only applied
    to "text", here called "string", and not to "visible_text".
    """
    return string
    if params["use_for"] not in ("Both", "Notebook &amp; Default"):
        return string
    articles = get_articles_from_input_message(
        string,
        total_max_length=params["max_summarization_length"],
    )
    altered = normalize_links_and_add_footer(text, articles)
    print(f"[hello_outside_world] modified: '{string}'")
    return string


def custom_css():
    """
    Returns a CSS string that gets appended to the CSS for the webui.
    """
    return ""


def custom_js():
    """
    Returns a javascript string that gets appended to the javascript
    for the webui.
    """
    return ""


def setup():
    """
    Gets executed only once, when the extension is imported.
    """
    nltk.download("punkt")
    pass


def ui():
    """
    Gets executed when the UI is drawn. Custom gradio elements and
    their corresponding event handlers should be defined here.

    To learn about gradio components, check out the docs:
    https://gradio.app/docs/
    """
    with gr.Accordion(label="🌎 Hello, Outside World!", css_class="hello-outside-world"):
        summary_length_trigger = gr.Number(
            label="When page content is under this length, the content will not be summarized.",
            value=params["summary_length_trigger"],
        )
        max_summarization_length = gr.Number(
            label="Maximum length of all summarizations. If the total length of summarizations exceeds this, it will fail",
            value=params["max_summarization_length"],
        )
        use_for = gr.Dropdown(
            USE_FOR_CHOICES,
            value=params["use_for"],
        )
        enable_visible = gr.Checkbox(
            label="Change the output of the message to include the summaries.",
            value=params["enable_visible"],
        )
        
        # Event handlers

        summary_length_trigger.change(
            lambda x: params.update({"summary_length_trigger": x}),
            summary_length_trigger,
            None,
        )
        max_summarization_length.change(
            lambda x: params.update({"max_summarization_length": x}),
            max_summarization_length,
            None,
        )
        use_for.change(lambda x: params.update({"use_for": x}), use_for, None)
        enable_visible.change(
            lambda x: params.update({"enable_visible": x}), enable_visible, None
        )
        save_params_btn = gr.Button("Save Settings")
        load_params_btn = gr.Button("Load Settings")
        save_params_btn.click(save_params)
        load_params_btn.click(load_params)
