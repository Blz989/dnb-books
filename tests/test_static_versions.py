import re

from django.templatetags.static import static
from django.urls import reverse

from core.storage import file_version


def test_static_urls_carry_a_content_hash():
    url = static("core/app.css")
    assert re.fullmatch(r"/static/core/app\.css\?v=[0-9a-f]{10}", url)


def test_hash_follows_the_file_contents(tmp_path):
    f = tmp_path / "a.css"
    f.write_text("a { color: red; }")
    first = file_version(str(f))
    f.write_text("a { color: blue; }")
    assert file_version(str(f)) != first and len(first) == 10


def test_base_page_links_versioned_css_and_js(client, standard_user):
    client.force_login(standard_user)
    html = client.get(reverse("home")).content.decode()
    assert re.search(r'href="/static/core/app\.css\?v=[0-9a-f]{10}"', html)
    assert re.search(r'src="/static/core/theme\.js\?v=[0-9a-f]{10}"', html)
