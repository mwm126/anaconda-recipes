#!/usr/bin/env python



"""
IMPORTANT: This script is for Continuum internal use only.


recipe_arbiter.py - Arbitration for conda package recipes among the ContinuumIO and AnacondaRecipes GitHub organizations

Usage:
- Offers four "actions":
    - quick-diff: View all public recipes that exist in either of the two
      organizations

    - diff: Like quick-diff, but provides additional information such as the
      most-recent date that the recipes were updated

    - push: Takes the latest "master" of the ContinuumIO public recipes, pushes
      each one onto a separate branch on their respective AnacondaRecipes repos,
      and opens a PR for each one

    - pull: Takes the latest "master" branches of the AnacondaRecipes
      repositories, and opens a PR on the ContinuumIO/anaconda repository

Implementation Details:
- Uses the GitHub API, ContinuumIO/anaconda repo, ContinuumIO/anaconda-recipes
  repo, and recipes under the AnacondaRecipes GitHub organization

- Requires GitHub username and password for Basic Authentication, but does not
  store them anywhere

Future improvements:
- We could remove the dependency on ContinuumIO/anaconda-recipes if there were
an alternative way to distinguish between public and private recipes

Tips:
- While developing, the -d/--debug feature is a big time-saver on repeat runs,
especially if the internet is slow.

TODOs:
- Handle the "truncated" case (grep for 'assert .*truncated')
"""



import sys
import argparse
import os
import time
import shutil
import logging
import getpass
import json
import datetime
from collections import deque

import git
import requests
import pandas as pd



# This domain is used in every GitHub REST API call
GH_API_URL = 'https://api.github.com'

# Prepare a requests Session object for connection-pooling and Keep-Alive
GH_SESSION = requests.Session()

# Directory which stores the cached responses
CACHE_DIR = '/tmp'

# Directory where Git repo(s) are checked out
WORK_DIR = '/tmp'


def cache_is_ready(*resp_types):
    """resp_types is a list of strings representing cache file basenames.
    Return True if cache is ready to be read.
    """
    for resp_type in resp_types:
        cache_fpath = os.path.join(CACHE_DIR, resp_type)+'.json'
        if not os.path.isfile(cache_fpath):
            return False
    return True
        

def write_responses_cache(**responses):
    """responses is a dict of string->dict, representing cache file basenames
    mapped to JSON responses (to be cached).
    """
    for resp_type, resp_json in responses.items():
        cache_fpath = os.path.join(CACHE_DIR, resp_type)+'.json'
        with open(cache_fpath+'.tmp', 'w') as fp:
            json.dump(resp_json, fp)
        shutil.move(cache_fpath+'.tmp', cache_fpath)
        logging.debug('Cached GitHub API response for "{}" as {}'.format(resp_type, cache_fpath))


def read_cached_responses(*resp_types):
    """resp_types is a list of strings representing cache file basenames.
    Return a tuple corresponding to resp_types, which represents the cached JSON
    responses.
    """
    cached_responses = []

    for resp_type in resp_types:
        cache_fpath = os.path.join(CACHE_DIR, resp_type)+'.json'
        with open(cache_fpath, 'r') as fp:
            cache_json = json.load(fp)
        cached_responses.append(cache_json)

    return tuple(cached_responses)


def raise_on_err(resp):
    """Raise an Exception if given requests Response object indicates failure.
    """
    if isinstance(resp, requests.models.Response) and not resp.ok:
        if hasattr(resp, 'reason'):
            raise Exception('GitHub API responded with reason: "{}"'.format(resp.reason))
        elif hasattr(resp, 'status_code'):
            raise Exception('GitHub API responded with status "{}"'.format(resp.status_code))
        else:
            raise Exception('GitHub API request failed')

    if isinstance(resp, dict) and 'message' in resp:
        raise Exception('GitHub API returned the following error message: "{}"'.format(resp['message']))


def should_exclude_name(file_name):
    """Return True if file_name doesn't match a valid conda recipe name, and
    therefore should be excluded.
    """
    return (file_name.endswith('BUILD') or
            file_name.endswith('.md') or
            file_name.endswith('.txt'))


def clone_repo(to_path, uri='ContinuumIO/anaconda'):
    """Clone repo into a fresh directory."""
    url = 'https://github.com/{}.git'.format(uri)

    if os.path.isdir(to_path):
        shutil.rmtree(to_path)

    class DisplayProgress(git.RemoteProgress):
        def update(self, op, cur_count, max_count=None, message=''):
            print('.', end='', flush=True)

    # Clone the repo
    progressDisplay = DisplayProgress()
    logging.info('Cloning {}'.format(url))
    repo = git.Repo.clone_from(url, to_path, progress=progressDisplay)
    print()

    return repo


def get_anaconda_recipes(quick=False):
    """Return a dictionary with keys as all AnacondaRecipes repository names,
    and values as all AnacondaRecipes repository specifications.

    If quick=False, the values are JSON objects representing the latest commit to
    each repository.
    """
    recipe_repos = {}

    logging.info('Querying AnacondaRecipe repositories...')

    # The GitHub API paginates on repository requests; here we find the number of pages
    pages_header = GH_SESSION.head(GH_API_URL+'/orgs/AnacondaRecipes/repos?page=1')
    raise_on_err(pages_header)
    npages = int(pages_header.links['last']['url'].rsplit('=', 1)[1])

    for page_ct in range(1, npages+1):
        public_repos_resp = GH_SESSION.get(GH_API_URL+'/orgs/AnacondaRecipes/repos?page='+str(page_ct))
        public_repos = public_repos_resp.json()
        raise_on_err(public_repos)
        for public_repo in public_repos:
            repo_name = public_repo['name']
            assert repo_name.endswith('-recipe')
            recipe_name = repo_name.split('-recipe', 1)[0]
            if not quick:
                commit_resp = GH_SESSION.get(GH_API_URL+'/repos/AnacondaRecipes/'+repo_name+'/commits')
                commit_json = commit_resp.json()
                raise_on_err(commit_json)
                sorted_commits = list(sorted(commit_json, key=lambda c: c['commit']['committer']['date']))
                latest_commit = sorted_commits[-1]
                recipe_repos[recipe_name] = latest_commit
            else:
                recipe_repos[recipe_name] = public_repo

    logging.info('Found {} repositories.'.format(len(recipe_repos)))

    return recipe_repos


def add_git_subtrees(repo, recipe_repos):
    """Add a git subtree for each AnacondaRecipe repository."""
    for repo_ct, (recipe_name, recipe_repo) in enumerate(recipe_repos.items()):
        repo_url = 'git://github.com/AnacondaRecipes/{}-recipe'.format(recipe_name)
        subtree_dest = os.path.join('prefix', recipe_name)

        # Add the git subtree as a distinct directory in the git repo
        logging.info('Adding subtree {}/{} at "{}" (-> {})'.format(repo_ct+1, len(recipe_repos), subtree_dest, repo_url))
        #repo.git.remote('add', recipe_name, repo_url)
        repo.git.subtree('add', repo_url, 'master', prefix=subtree_dest, annotate='[anaconda-recipes]')


def set_gh_auth():
    """Prompt for user's GitHub username and password. Update the global
    requests Session object with this information so that we don't have to pass
    around credentials.
    """
    global GH_SESSION
    gh_username = input('GitHub username: ')
    gh_password = getpass.getpass(prompt='GitHub password: ')
    GH_SESSION.auth = (gh_username, gh_password)


def get_anaconda_dist_packages(quick=False):
    """Return a dictionary with keys as all Anaconda distribution recipe names,
    and values as all Anaconda distribution recipe specifications.

    If quick=False, the values are JSON objects representing the latest commit to
    each repository.
    """
    dist_packages = {}

    # Get the SHA for latest packages directory:
    logging.info('Fetching all Anaconda distribution recipes...')
    tree_resp = GH_SESSION.get(GH_API_URL+'/repos/ContinuumIO/anaconda/git/trees/master')
    tree_json = tree_resp.json()
    raise_on_err(tree_json)
    assert not tree_json['truncated'] # This means we have over 1K files in root anaconda dir
    packages_sha = None
    for leaf in tree_json['tree']:
        if leaf['path'] == 'packages':
            packages_sha = leaf['sha']
            break

    # Get the packages names and use these to get Anaconda distribution package info
    package_tree_resp = GH_SESSION.get(GH_API_URL+'/repos/ContinuumIO/anaconda/git/trees/'+packages_sha)
    package_tree_json = package_tree_resp.json()
    raise_on_err(package_tree_json)
    assert not package_tree_json['truncated'] # This means we have over 1K packages in distribution
    dist_packages = {leaf['path']: leaf for leaf in package_tree_json['tree']}
    logging.info('Found {} distribution recipes.'.format(len(dist_packages)))


    logging.info('Fetching public Anaconda distribution recipes...')
    dist_public_packages = {}
    public_package_tree_resp = GH_SESSION.get(GH_API_URL+'/repos/ContinuumIO/anaconda-recipes/git/trees/master')
    public_package_tree_json = public_package_tree_resp.json()
    raise_on_err(public_package_tree_json)
    assert not public_package_tree_json['truncated'] # This means we have over 1K packages in public repo
    public_package_names = {leaf['path']: leaf for leaf in public_package_tree_json['tree']}

    # Find the earliest commit for the packages directory. Use this as the "default" when GitHub returns
    # an empty list on the commits of a subdirectory.
    commit_resp = GH_SESSION.get(GH_API_URL+'/repos/ContinuumIO/anaconda/commits?path=packages')
    commit_json = commit_resp.json()
    raise_on_err(commit_json)
    sorted_commits = list(sorted(commit_json, key=lambda c: c['commit']['committer']['date']))
    default_commit = sorted_commits[0]

    for package_ct, package_name in enumerate(public_package_names):
        if should_exclude_name(package_name):
            continue

        # Get the commit data for each public Anaconda recipe
        if not quick:
            commit_resp = GH_SESSION.get(GH_API_URL+'/repos/ContinuumIO/anaconda/commits?path=packages/'+package_name)
            commit_json = commit_resp.json()
            raise_on_err(commit_json)
            sorted_commits = list(sorted(commit_json, key=lambda c: c['commit']['committer']['date']))
            if not sorted_commits:
                latest_commit = default_commit
            else:
                latest_commit = sorted_commits[-1]
            dist_public_packages[package_name] = latest_commit
        else:
            dist_public_packages[package_name] = public_package_names[package_name]


    logging.info('Found {} public distribution recipes.'.format(len(dist_public_packages)))


    return dist_packages, dist_public_packages


def calc_recipe_diff(public_recipes, dist_public_recipes, quick=False):
    """Return a Pandas dataframe object representing the diff between public_recipes and dist_public_recipes.
    """
    logging.info('Calculating recipe differences...')

    public_repo_names = set(public_recipes)
    dist_repo_names = set(dist_public_recipes)
    new_external_recipes = public_repo_names - dist_repo_names
    new_internal_recipes = dist_repo_names - public_repo_names
    common_recipes = dist_repo_names.intersection(public_repo_names)

    recipes = sorted(dist_repo_names.union(public_repo_names))
    df = pd.DataFrame(columns=['Recipe', 'AnacondaRecipes', 'ContinuumIO'])
    df['Recipe'] = recipes
    if quick:
        df['AnacondaRecipes'] = df['Recipe'].map(lambda r: r in public_recipes)
        df['ContinuumIO'] = df['Recipe'].map(lambda r: r in dist_public_recipes)
    else:
        df['AnacondaRecipes'] = df['Recipe'].map(lambda r: public_recipes[r]['commit']['committer']['date'] if r in public_recipes else pd.np.nan)
        df['ContinuumIO'] = df['Recipe'].map(lambda r: dist_public_recipes[r]['commit']['committer']['date'] if r in dist_public_recipes else pd.np.nan)

    return df


def get_recipe_diff(action, debug=False):
    """Return a Pandas dataframe representing a "diff" between AnacondaRecipes
    and ContinuumIO organizations' views of public conda packages.

    action can be one of the following: ('quick-diff', 'diff')
    """
    response_types = 'public_recipes', 'dist_recipes', 'dist_public_recipes'
    if debug and cache_is_ready(*response_types):
        logging.info('Reading cached GitHub API responses.')
        public_recipes, dist_recipes, dist_public_recipes = read_cached_responses(*response_types)
    else:
        set_gh_auth() # Fixes "API rate limit exceeded" issue
        public_recipes = get_anaconda_recipes(quick=('quick' in action))
        dist_recipes, dist_public_recipes = get_anaconda_dist_packages(quick=('quick' in action))
        write_responses_cache(public_recipes, dist_recipes, dist_public_recipes)

    logging.info('AnacondaRecipes repos: {}'.format(len(public_recipes)))
    logging.info('anaconda-recipes (mirrored) recipes: {}'.format(len(dist_public_recipes)))
    logging.info('Anaconda Distribution (open source + internal) recipes: {}'.format(len(dist_recipes)))

    diff_df = calc_recipe_diff(public_recipes, dist_public_recipes, quick=('quick' in action))
    return diff_df


def complete_action(action, debug=False):
    """Return an integer status code - 0 meaning success, nonzero meaning
    failure - after executing user-requested action.

    action can be one of the following: ('quick-diff', 'diff', 'push', 'pull')
    debug=True enables the GitHub API response cache.
    """
    logging.info('Executing action "{}".'.format(action))
    if debug:
        logging.info('Debug mode enabled.')

    if action in ('quick-diff', 'diff'):
        diff_df = get_recipe_diff(action, debug=debug)
        logging.info('\n'+str(diff_df))
    else:
        set_gh_auth() # Fixes "API rate limit exceeded" issue
        public_recipes = get_anaconda_recipes(quick=True)

        repo = clone_repo(os.path.join(WORK_DIR, 'anaconda'))
        branch_name = 'AR_{}_{}'.format(action, str(datetime.datetime.now().date()))
        repo.git.checkout('HEAD', b=branch_name)

        if action == 'push':
            # TODO: Use the GitHub API to open PRs on each AnacondaRecipes repository
            pass
        elif action == 'pull':
            # TODO: Open a PR with a new branch name, representing all externally-altered recipes
            add_git_subtrees(repo, public_recipes)
        else:
            raise Exception('If the code reaches this point, the argument parsing logic needs to be corrected.')

    return 0



def main(argv):
    """Parse command-line args, handle requested user actions. Return an integer
    status code.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('quick-diff', 'diff', 'pull', 'push'), help='Action')
    parser.add_argument('-l', '--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO', help='Logging level')
    parser.add_argument('-d', '--debug', action='store_true', help='Speed up debugging by caching GitHub API responses on disk, and reading these responses on future runs in --debug mode. This also avoids user/pass prompt after cache is created.')
    args = parser.parse_args(argv[1:])

    logging.basicConfig(format='[%(asctime)s] %(levelname)s [%(module)s]: %(message)s', level=getattr(logging, args.log_level))

    status_code = complete_action(args.action, debug=args.debug)

    return status_code



if __name__ == '__main__':
    sys.exit(main(sys.argv))
