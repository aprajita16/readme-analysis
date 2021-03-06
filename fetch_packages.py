#! /usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import unicode_literals
import logging
import slumber
import argparse
import base64
import re
import time
import requests
from bs4 import BeautifulSoup
import json

from peewee import fn
from models import NPMPackage, create_npm_tables, PyPIPackage, create_pypi_tables
from api import LibrariesIo, Github, default_requests_session


logging.basicConfig(level=logging.INFO, format="%(message)s")
github = Github()
libraries_io = LibrariesIo()


def make_request(method, *args, **kwargs):

    MAX_ATTEMPTS = 5
    RETRY_DELAY = 30
    try_again = True
    attempts = 0
    res = None

    def log_error(err_msg):
        logging.warn(
            "Error (%s) For API call %s, Args: %s, Kwargs: %s",
            str(err_msg), str(method), str(args), str(kwargs)
        )

    while try_again and attempts < MAX_ATTEMPTS:
        try:
            res = method(*args, **kwargs)
            if hasattr(res, 'status_code') and res.status_code not in [200]:
                log_error(str(res.status_code))
                res = None
            try_again = False
        except (slumber.exceptions.HttpNotFoundError):
            log_error("Not Found")
            try_again = False
        except slumber.exceptions.HttpServerError:
            log_error("Server 500")
            try_again = False
        except requests.exceptions.ConnectionError:
            log_error("ConnectionError")
        except requests.exceptions.ReadTimeout:
            log_error("ReadTimeout")

        if try_again:
            logging.warn("Waiting %d seconds for before retrying.", int(RETRY_DELAY))
            time.sleep(RETRY_DELAY)
            attempts += 1

    return res


def fetch_packagenames_from_libraryio(package_count):
    # Currently only fetchers packages in multiples of 30
    # If package_count == -1, then all packages will be fetched

    keep_fetching = True
    page_no = 0
    count_retrieved = 0

    while keep_fetching:

        logging.info("Fetching page %d of package names", page_no)
        packages = make_request(
            libraries_io.search.get,
            q='', platforms='NPM', page=page_no
        )

        if packages is not None:
            for p in packages:
                Package.get_or_create(
                    name=p['name'],
                    repository_url=p['repository_url'],
                    page_no=page_no,
                )

        count_retrieved += len(packages)
        page_no += 1
        keep_fetching = (
            (len(packages) > 0) and
            (package_count == -1 or count_retrieved < package_count)
        )


def github_info(url):
    if 'github.com' in url:
        url = re.sub('^.*(?:github.com)', '', url)
        # Repo name can have slashes, so split on the first two slashes
        _, user, repo_name = url.split('/', 2)
        return (user, repo_name)
    return None


def fetch_github_readmes(packages):
    for p in packages:
        gh_info = github_info(p.repository_url)
        if gh_info is None:
            logging.info(
                "Not fetching README.  " +
                "Package %s does not have a Github repository",
                p.name)
            continue
        user, repo_name = gh_info

        logging.info("Fetching README for %s", p.name)
        readme = make_request(github.repos(user)(repo_name).readme.get)
        if readme is not None:
            p.readme = base64.b64decode(readme['content'])
            p.save()


def fetch_github_stats(packages):
    for p in packages:
        gh_info = github_info(p.repository_url)
        if gh_info is None:
            logging.info(
                "Not fetching stats.  " +
                "Package %s does not have a Github repository",
                p.name)
            continue
        user, repo_name = gh_info

        logging.info("Fetching stats for package %s", p.name)
        repo_info = make_request(
            libraries_io.github(user)(repo_name).get()
        )
        if repo_info is not None:
            p.stargazers_count = repo_info['stargazers_count']
            p.forks_count = repo_info['forks_count']
            p.open_issues_count = repo_info['open_issues_count']
            p.subscribers_count = repo_info['subscribers_count']
            p.github_contributions_count = repo_info['github_contributions_count']
            p.has_wiki = repo_info['has_wiki']
            p.save()


def fetch_npm_package_list():
    res = make_request(
        default_requests_session.get,
        "https://skimdb.npmjs.com/registry/_all_docs",
    )
    if res is not None:
        packages = res.json()['rows']
        for p in packages:
            NPMPackage.get_or_create(name=p['id'])


def fetch_npm_data(packages):
    to_count = lambda s: int(s.replace(',', '')) if s != '' else 0
    for p in packages:
        res = make_request(
            default_requests_session.get,
            "https://www.npmjs.com/package/{pkg}".format(pkg=p.name),
        )

        if res is not None:
            page = BeautifulSoup(res.content, 'html.parser')
            p.readme = str(page.select('div#readme')[0])
            p.description = page.select('p.package-description')[0].text
            p.day_download_count = to_count(page.select('strong.daily-downloads')[0].text)
            p.week_download_count = to_count(page.select('strong.weekly-downloads')[0].text)
            p.month_download_count = to_count(page.select('strong.monthly-downloads')[0].text)
            p.dependents = json.dumps(
                [e.text for e in page.select('p.dependents a')])
            p.dependencies = json.dumps(
                [e.text for e in page.select('p.list-of-links:nth-of-type(2) a')])
            p.save()


def fetch_pypi_package_list():
    res = make_request(
        default_requests_session.get,
        "https://pypi.python.org/pypi?%3Aaction=index",
    )

    # Use BeautifulSoup to parse the HTML and find the package names.
    page = BeautifulSoup(res.content, 'html.parser')
    package_table = page.find('table')
    all_rows = package_table.findAll('tr')

    logging.info("====There are currently %d packages on PyPI.", len(all_rows) - 1)

    num_fetched = 0

    # Each row represents a single PyPI package.
    for row in all_rows:
        # Each row has 2 columns, a name (hyperlinked) and a description.
        link = row.find('a')
        # Only fetch package if it has a link to its own page.
        if link is not None:
            # Reformat spacing in extracted name.
            package_name = link.text.replace(u'\xa0', ' ')
            PyPIPackage.get_or_create(name=package_name)
            num_fetched += 1
            if num_fetched % 10 == 0:
                logging.info("%d packages fetched.", num_fetched)

    logging.info("====Done fetching package list. There were %d packages.", num_fetched)


def fetch_pypi_data(packages):
    logging.info("Fetching PyPI data for %d packages", len(packages))

    num_fetched = 0

    for p in packages:
        # Turn package name into correct URL suffix.
        # Assumes p.name does not have invalid characters for a URL, such as "
        formatted_name = p.name.replace(' ', '/').replace(u'\xa0', '/')

        res = make_request(
            default_requests_session.get,
            "https://pypi.python.org/pypi/{pkg}/json".format(pkg=formatted_name),
        )

        if res is not None:
            try:
                package_json = res.json()
            except ValueError:
                logging.warn("No JSON Object Could Be Decoded")

            package_info = package_json['info']

            download_info = package_info['downloads']
            p.day_download_count = download_info['last_day']
            p.week_download_count = download_info['last_week']
            p.month_download_count = download_info['last_month']

            # The summary key is the brief description of the package, in just a few lines.
            p.description = package_info['summary']
            # The description key includes details such as installation, usage, and examples.
            p.readme = package_info['description']

            p.save()

            num_fetched += 1
            if num_fetched % 10 == 0:
                logging.info("Done fetching %d packages.", num_fetched)

    logging.info("====Done retrieving package data for all packages. %d packages had data.", num_fetched)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Download package stats for NPM or PyPI")
    parser.add_argument(
        '--db',
        help="which package database to scrape and analyze; valid arguments are 'npm' or 'pypi'"
    )
    parser.add_argument(
        '--package-list',
        action='store_true',
        help="fetch package list"
    )
    parser.add_argument(
        '--data',
        action='store_true',
        help="fetch package data (READMES and downloads)"
    )
    parser.add_argument(
        '--update',
        action='store_true',
        help="only update existing data"
    )
    parser.add_argument(
        '--lib-packages',
        action='store_true',
        help="fetch package names from Library.IO (only applicable to npm)"
    )
    parser.add_argument(
        '--lib-package-count',
        type=int,
        default=-1,
        help="how many package names to fetch (only applicable to npm)"
    )
    parser.add_argument(
        '--github-readmes',
        action='store_true',
        help="fetch Github READMEs (only applicable to npm)"
    )
    parser.add_argument(
        '--github-stats',
        action='store_true',
        help="fetch Github stats (only applicable to npm)"
    )
    args = parser.parse_args()


    if args.db == 'npm':
        if args.package_list:
            create_npm_tables()
            fetch_npm_package_list()
        if args.data:
            if args.update:
                packages = NPMPackage.select().where(NPMPackage.description != '')
            else:
                packages = NPMPackage.select().where(NPMPackage.readme >> None).order_by(fn.Random())
            fetch_npm_data(packages)
        if args.lib_packages:
            create_tables()
            fetch_packagenames_from_libraryio(args.lib_package_count)
        if args.github_readmes:
            fetch_github_readmes(NPMPackage.select())
        if args.github_stats:
            fetch_github_stats(NPMPackage.select())
    elif args.db == 'pypi':
        if args.package_list:
            create_pypi_tables()
            fetch_pypi_package_list()
        if args.data:
            if args.update:
                packages = PyPIPackage.select().where(PyPIPackage.description != '')
            else:
                packages = PyPIPackage.select().where(PyPIPackage.readme >> None).order_by(fn.Random())
            fetch_pypi_data(packages)
    else:
        print "Please provide a valid argument to package-list: 'npm' or 'pypi'"
