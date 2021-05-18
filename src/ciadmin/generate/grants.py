# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.

import re

from tcadmin.resources import Role
from tcadmin.util.scopes import normalizeScopes

from .ciconfig.environment import Environment
from .ciconfig.grants import Grant, GroupGrantee, ProjectGrantee, RoleGrantee
from .ciconfig.projects import Project

LEVEL_PRIORITIES = {1: "low", 2: "low", 3: "highest"}


def add_scopes_for_projects(grant, grantee, add_scope, projects):
    def match(grantee_values, proj_value):
        if grantee_values is None:
            return True
        if any(proj_value == grantee_value for grantee_value in grantee_values):
            return True
        return False

    def feature_match(features, project):
        if features is None:
            return True
        for feature in features:
            if feature.startswith("!"):
                if project.feature(feature[1:]):
                    return False
            else:
                if not project.feature(feature):
                    return False
        return True

    for project in projects:
        if not match(grantee.access, project.access):
            continue
        if not match(grantee.level, project.get_level()):
            continue
        if not match(grantee.alias, project.alias):
            continue
        if not feature_match(grantee.feature, project):
            continue
        if grantee.is_try is not None:
            if project.is_try != grantee.is_try:
                continue
        if not match(grantee.trust_domain, project.trust_domain):
            continue

        jobs = grantee.job
        if "*" in jobs and project.repo_type == "git" and project.level != 1:
            # Github mixes pull-requests and other tasks under the same prefix
            # Since pull-requests should be level-1, we need to explicitly
            # split based on the job
            jobs = [job for job in jobs if job != "*"]
            jobs += ["pull-request", "branch:*", "release", "cron:*", "action:*"]

        # Only grant scopes to `cron:` or `action:` jobs if the corresponding features
        # are enabled. This allows having generic grants that don't geneate unused roles
        if not project.feature("taskgraph-cron") and not project.feature("gecko-cron"):
            jobs = [job for job in jobs if not job.startswith("cron:")]
        if not project.feature("taskgraph-actions") and not project.feature(
            "gecko-actions"
        ):
            jobs = [job for job in jobs if not job.startswith("action:")]
        if project.repo_type != "git" or not grantee.include_pull_requests:
            jobs = [job for job in jobs if job != "pull-request"]

        # ok, this project matches!
        for job in jobs:
            roleId = "{}:{}".format(project.role_prefix, job)

            # perform substitutions as grants.yml describes
            subs = {}
            subs["alias"] = project.alias
            if project.trust_domain:
                subs["trust_domain"] = project.trust_domain
            if project.trust_project:
                subs["trust_project"] = project.trust_project
            level = project.get_level()
            if level is not None:
                subs["level"] = project.level
                # In order to avoid granting pull-requests graphs
                # access to the level-3 workers, we overwrite their value here
                if job == "pull-request":
                    subs["level"] = 1
                subs["priority"] = LEVEL_PRIORITIES[project.level]
            try:
                subs["repo_path"] = project.repo_path
            except AttributeError:
                pass  # not an known supported repo..

            for scope in grant.scopes:
                add_scope(roleId, scope.format(**subs))


def add_scopes_for_groups(grant, grantee, add_scope):
    for group in grantee.groups:
        roleId = "project:releng:ci-group:{}".format(group)
        for scope in grant.scopes:
            # use an empty format() to catch any stray {..} in the scope
            add_scope(roleId, scope.format())


def add_scopes_for_roles(grant, grantee, add_scope):
    for role in grantee.roles:
        for scope in grant.scopes:
            # use an empty format() to catch any stray {..} in the scope
            add_scope(role, scope.format())


async def update_resources(resources):
    """
    Manage the scopes granted to projects.  This file interprets `grants.yml`
    in ci-configuration. Its behavior is largely documented in the comment in that file.
    """

    grants = await Grant.fetch_all()
    projects = await Project.fetch_all()
    environment = await Environment.current()

    # manage our resources..
    if environment.name != "legacy":
        resources.manage("Role=mozilla-group:.*")
        resources.manage("Role=mozillians-group:.*")
        resources.manage("Role=login-identity:.*")
        resources.manage("Role=hook-id:.*")
        resources.manage("Role=project:.*")
        resources.manage("Role=repo:.*")
    else:
        resources.manage("Role=project:releng:ci-group:.*")
        resources.manage("Role=repo:hg.mozilla.org/.*")
        # TODO: once we stabilize ci-admin in the github world, we should be
        # authorative for various github *org/users* vs. individual repos.
        for project in projects:
            if project.repo_type == "git":
                resources.manage("Role={}:.*".format(project.role_prefix))

    # calculate scopes..
    roles = {}

    def add_scope(roleId, scope):
        roles.setdefault(roleId, []).append(scope)

    for grant in grants:
        if grant.environments and environment.name not in grant.environments:
            # skip grant for this environment
            continue
        for grantee in grant.grantees:
            if isinstance(grantee, ProjectGrantee):
                add_scopes_for_projects(grant, grantee, add_scope, projects)
            elif isinstance(grantee, GroupGrantee):
                add_scopes_for_groups(grant, grantee, add_scope)
            elif isinstance(grantee, RoleGrantee):
                add_scopes_for_roles(grant, grantee, add_scope)
            else:
                raise RuntimeError("unknown grantee!")

    # ..and add the roles
    for roleId, scopes in roles.items():
        resources.manage("Role={}".format(re.escape(roleId)))
        role = Role(
            roleId=roleId,
            scopes=normalizeScopes(scopes),
            description="Scopes in this role are defined in "
            "[ci-configuration/grants.yml]"
            "(https://hg.mozilla.org/ci/ci-configuration/file/tip/grants.yml).",
        )
        resources.add(role)