import sys
import os
import re
import hmac
import json
import logging
import argparse
from hashlib import sha1
import requests
from twisted.web import resource, server
from twisted.internet import reactor, endpoints

import icons
import database

DEBUG = False

# DB Core
DB = database.DBCore()

# Setup logging
log_format = "[%(asctime)s]: %(message)s"
datefmt = "%Y-%m-%d %H:%M:%S"
logging_level = logging.INFO
if DEBUG:
    logging_level = logging.NOTSET
logging.basicConfig(
    filename='events.log',
    level=logging_level,
    format=log_format,
    datefmt=datefmt
)

console = logging.StreamHandler()
console.setLevel(logging_level)
console.setFormatter(logging.Formatter(log_format, datefmt))
logging.getLogger('').addHandler(console)

logger = logging.getLogger(__name__)


def log_message(message):
    """Logs a message to a file and prints on screen"""
    logger.info(message)


def handle_exception(exc_type, exc_value, exc_traceback):
    """Makes exception log to the logger"""
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = handle_exception


# Setup the config
class Config:
    def load_variable(self, environ_name, alt_environ_name, config_value):
        if alt_environ_name is not None and os.environ.get(alt_environ_name) is not None:
            return os.environ.get(alt_environ_name)
        if os.environ.get(environ_name) is not None:
            return os.environ.get(environ_name)
        return config_value

    def __init__(self):
        config = {}
        if os.path.exists(os.path.abspath('config.json')):
            with open('config.json', 'r') as f:
                config = json.load(f)
        else:
            log_message("Make sure the config file exists.")

        # Overwrite that with the environment variables
        if os.environ.get('ICONBOT_IGNORELIST') is not None:
            config['ignore'] = os.environ.get('ICONBOT_IGNORELIST').split(',')
        self.webhook_port = Config.load_variable(self, 'ICONBOT_WEBHOOK_PORT', None, config.get('webhook_port'))
        self.github_secret = Config.load_variable(self, 'GITHUB_SECRET', 'ICONBOT_GITHUB_SECRET',
                                                  config.get('github', {}).get('secret')).encode('utf-8')
        self.github_user = Config.load_variable(self, 'GITHUB_USER', 'ICONBOT_GITHUB_USER',
                                                config.get('github', {}).get('user'))
        self.github_auth = Config.load_variable(self, 'GITHUB_AUTH', 'ICONBOT_GITHUB_AUTH',
                                                config.get('github', {}).get('auth'))
        self.upload_api_url = Config.load_variable(self, 'UPLOADAPI_URL', 'UPLOADAPI_URL',
                                                   config.get('upload_api', {}).get('url'))
        self.upload_api_key = Config.load_variable(self, 'UPLOADAPI_KEY', 'UPLOADAPI_KEY',
                                                   config.get('upload_api', {}).get('key'))
        self.ignore_list = config['ignore']


config = Config()
actions_to_check = ['opened', 'synchronize']
binary_regex = re.compile(r'diff --git a\/(.*\.dmi) b\/.?')


def compare_secret(secret_to_compare, payload):
    """Compares given secret with ours"""
    if secret_to_compare is None:
        return False

    this_secret = hmac.new(config.github_secret, payload, sha1)
    secret_to_compare = secret_to_compare.replace('sha1=', '')
    return hmac.compare_digest(secret_to_compare, this_secret.hexdigest())


def check_diff(diff_url):
    """Checks the diff url for icons"""
    req = requests.get(diff_url)
    if req.status_code == 404:
        return None
    diff = req.text.split('\n')
    icons_with_diff = []
    for line in diff:
        match = binary_regex.search(line)
        if not match:
            continue
        icons_with_diff.append(match.group(1))
    return icons_with_diff


def upload_image(file_to_upload, img_hash, upload=True):
    """Uploads an image to the configured host"""
    if not upload:
        return None
    has_link = DB.get_url(img_hash)
    if has_link is not None:
        return has_link
    data = {'key': config.upload_api_key}
    files = {'file': file_to_upload}
    req = requests.post(config.upload_api_url, data=data, files=files)
    url = req.json()['url']
    DB.set_url(img_hash, url)
    return url


def check_comments(api_url):
    """Checks all comments on given issue if we already commented on it(using config's github username), will return the issue ID if exists"""
    req = requests.get(api_url)
    if req.status_code == 200:
        for comment in req.json():
            if comment['user']['login'] == config.github_user:
                return comment['url']
    return None


def post_comment(issue_url, message_dict, base):
    """Post a comment on given github issue url"""
    repo_name = base['repo']['full_name']
    github_api_url = "{issue}/comments".format(issue=issue_url)
    comment_id = check_comments(github_api_url)
    http_method = requests.post
    if comment_id is not None:
        github_api_url = comment_id
        http_method = requests.patch
        log_message("[{}] Found a comment from us on the pull request; {}".format(repo_name, github_api_url))
    body = json.dumps({'body': '\n'.join(message_dict)})
    req = http_method(github_api_url, data=body, auth=(config.github_user, config.github_auth))
    if req.status_code == 201 or req.status_code == 200:
        log_message("[{}] Sucessefully commented icon diff on: {}".format(repo_name, req.json()['html_url']))
    else:
        log_message("[{}] Failed to comment on: {}".format(repo_name, issue_url))
        log_message("Error code: {}".format(req.status_code))


def check_icons(icons_with_diff, base, head, issue_url, send_message=True):
    """
    Checks two icons for their states, comparing the images and posting on the PR in case
    a diff exists
    """
    if not os.path.exists('./icon_dump'):
        os.makedirs('./icon_dump')
    base_repo_url = base.get('repo').get('html_url')
    head_repo_url = head.get('repo').get('html_url')
    msgs = ["Icons with diff:"]
    req_data = {'raw': 1}
    if DEBUG:
        issue_number = re.sub(r'.*\/issues\/(\d*)', '\\1', issue_url)
    for icon in icons_with_diff:
        i_name = re.sub(r'.*\/(.*)\.dmi', '\\1', icon)
        icon_path_a = './icon_dump/old_{}.dmi'.format(i_name)
        icon_path_b = './icon_dump/new_{}.dmi'.format(i_name)
        response_a = requests.get('{}/blob/{}/{}'.format(base_repo_url, base['ref'], icon), data=req_data)
        response_b = requests.get('{}/blob/{}/{}'.format(head_repo_url, head['ref'], icon), data=req_data)
        if response_a.status_code == 200:
            with open(icon_path_a, 'wb') as f:
                f.write(response_a.content)
        elif response_a.status_code == 404:
            icon_path_a = ''
        if response_b.status_code == 200:
            with open(icon_path_b, 'wb') as f:
                f.write(response_b.content)
        # This means the file is being deleted, which does not interest us
        elif response_b.status_code == 404:
            try:
                os.remove(icon_path_a)
                continue
            except OSError:
                continue
        this_dict = icons.compare_two_icon_files(icon_path_a, icon_path_b)
        if not this_dict:
            continue
        msg = ["<details><summary>{}</summary>\n".format(icon), "Key | Old | New | Status", "--- | --- | --- | ---"]
        for key in this_dict:
            status = this_dict[key].get("status")
            if status == 'Equal':
                continue
            path_a = './icon_dump/old_{}.png'.format(key)
            path_b = './icon_dump/new_{}.png'.format(key)
            img_a = this_dict[key].get('img_a')
            if img_a:
                img_a.save(path_a)
                a_hash = this_dict[key].get('img_a_hash')
                with open(path_a, 'rb') as f:
                    url_a = "![{key}]({url})".format(key=key, url=upload_image(f, a_hash, send_message))
                if not DEBUG:
                    os.remove(path_a)
            else:
                url_a = "![]()"
            img_b = this_dict[key].get('img_b')
            if img_b:
                img_b.save(path_b)
                b_hash = this_dict[key].get('img_b_hash')
                with open(path_b, 'rb') as f:
                    url_b = "![{key}]({url})".format(key=key, url=upload_image(f, b_hash, send_message))
                if not DEBUG:
                    os.remove(path_b)
            else:
                url_b = "![]()"
            msg.append("{key}|{url_a}|{url_b}|{status}".format(key=key, url_a=url_a, url_b=url_b, status=status))
        msg.append("</details>")
        if (len(msg) > 4):
            msgs.append("\n".join(msg))
        if DEBUG:
            with open("icon_dump/{}_{}.log".format(i_name, issue_number), 'w') as fp:
                fp.write("\n".join(msg))
        else:
            if os.path.exists(icon_path_a):
                os.remove(icon_path_a)
            if os.path.exists(icon_path_b):
                os.remove(icon_path_b)
    if send_message and len(msgs) > 1:
        post_comment(issue_url, msgs, base)


class Handler(resource.Resource):
    """Opens a web server to handle POST requests on given port"""
    isLeaf = True

    def render_POST(self, request):
        payload = request.content.getvalue()
        if not compare_secret(request.getHeader('X-Hub-Signature'), payload):
            request.setResponseCode(401)
            log_message("POST received with wrong secret.")
            return b"Secret does not match."
        event = request.getHeader('X-GitHub-Event')
        if event != 'pull_request':
            request.setResponseCode(404)
            log_message("POST received with event: {}".format(event))
            return b"Event not supported"

        # Then we check the PR for icon diffs
        payload = json.loads("".join(map(chr, payload)))
        request.setResponseCode(200)
        pr_obj = payload['pull_request']
        if payload['action'] not in actions_to_check:
            return b"Not actionable"
        if pr_obj['user']['login'].lower() in config.ignore_list:
            return b"Ok"
        issue_url = pr_obj['issue_url']
        pr_diff_url = pr_obj['diff_url']
        head = pr_obj['head']
        base = pr_obj['base']
        # if payload['action'] == 'synchronize':
        #    pr_diff_url = "{html_url}/commits/{sha}.patch".format(html_url=pr_obj['html_url'], sha=head['sha'])
        icons_with_diff = check_diff(pr_diff_url)
        if icons_with_diff:
            log_message(
                "{}: Icon diff detected on pull request: {}!".format(base['repo']['full_name'], payload['number']))
            check_icons(icons_with_diff, base, head, issue_url)
        return b"Ok"

    def render_GET(self, request):
        request.setResponseCode(404)
        return b"GET requests are not supported."


def test_pr(number, owner, repository, send_message=False):
    """tests a pr for the icon diff"""
    req = requests.get("https://api.github.com/repos/{}/{}/pulls/{}".format(owner, repository, number))
    log_message("[{}/{}] Testing PR #{}".format(owner, repository, number))
    if req.status_code == 404:
        log_message('PR #{} on {}/{} does not exist.'.format(number, owner, repository))
        return
    payload = req.json()
    icons_diff = check_diff(payload['diff_url'])
    if not icons_diff:
        log_message("No diff detected on [{}/{}] #{}".format(owner, repository, number))
        return
    log_message("Icons:")
    for ic_name in icons_diff:
        log_message(ic_name)
    check_icons(icons_diff, payload['base'], payload['head'], payload['issue_url'], send_message)


def get_debug_input():
    owner = input("Owner: ")
    repo = input("Repo: ")
    number = input("PR number: ")
    send_msg = True if input("Send message(y/n): ")[:1].lower() == 'y' else False
    test_pr(number, owner, repo, send_msg)


def bulk_prs():
    org = 'tgstation'
    repo = 'tgstation'
    prs = []
    with open('bulk_prs.txt') as f:
        prs = f.readlines()

    # Check if the first line is a pr or a repo definition
    if " " in prs[0]:
        org = prs[0].split()[0]
        repo = prs[0].split()[1]
        prs.pop(0)

    for pr in prs:
        test_pr(int(pr), org, repo, True)


def start_server():
    """Starts the webserver"""
    webhook_port = "tcp:{}".format(config.webhook_port)
    endpoints.serverFromString(reactor, webhook_port).listen(server.Site(Handler()))
    log_message("Listening for requests on port: {}".format(config.webhook_port))
    try:
        reactor.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--mode", choices=['server', 'bulk', 'debug'], default='server')
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        DEBUG = True
    if args.mode == "debug":
        get_debug_input()
    elif args.mode == "bulk":
        bulk_prs()
    else:
        start_server()