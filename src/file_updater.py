#!/usr/bin/env python3

import collections
import dataclasses
import datetime
import json
import logging
import os
import pathlib
from typing import Dict, Set

from file_formatter import Formatter
from playlist_id import PlaylistID
from spotify import Spotify
from url import URL

logger: logging.Logger = logging.getLogger(__name__)


class FileUpdater:
    @classmethod
    async def update_files(cls, now: datetime.datetime, prod: bool) -> None:
        # Check nonempty to fail fast
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        assert client_id and client_secret

        # Initialize the Spotify client
        access_token = await Spotify.get_access_token(client_id, client_secret)
        spotify = Spotify(access_token)
        try:
            await cls._update_files_impl(now, prod, spotify)
        finally:
            await spotify.shutdown()

    @classmethod
    async def _update_files_impl(
        cls, now: datetime.datetime, prod: bool, spotify: Spotify
    ) -> None:
        # Relative to project root
        playlists_dir = "playlists" if prod else "_playlists"
        registry_dir = f"{playlists_dir}/registry"
        plain_dir = f"{playlists_dir}/plain"
        pretty_dir = f"{playlists_dir}/pretty"
        cumulative_dir = f"{playlists_dir}/cumulative"

        # Ensure the directories exist
        for path in [
            registry_dir,
            plain_dir,
            pretty_dir,
            cumulative_dir,
        ]:
            pathlib.Path(path).mkdir(parents=True, exist_ok=True)

        # Determine which playlists to scrape from the files in
        # playlists/registry. This makes it easy to add new a playlist: just
        # touch an empty file like playlists/registry/<playlist_id> and this
        # script will handle the rest.
        playlist_ids = {PlaylistID(x) for x in os.listdir(registry_dir)}

        # Aliases are alternative playlists names. They're useful for avoiding
        # naming collisions when archiving personalized playlists, which have the
        # same name for every user. To add an alias, add a single line
        # containing the desired name to playlists/registry/<playlist_id>
        aliases: Dict[PlaylistID, str] = {}
        for playlist_id in playlist_ids:
            registry_path = "{}/{}".format(registry_dir, playlist_id)
            with open(registry_path, "r") as f:
                alias_lines = f.read().splitlines()
            if not alias_lines:
                continue
            if len(alias_lines) != 1:
                raise Exception(f"Malformed alias: {playlist_id}")
            alias = alias_lines[0]
            # GitHub makes it easy to create single-line files that look empty
            # but actually contain a single newline. Normalize those files and
            # ignore the empty alias.
            if not alias:
                logger.info(f"Truncating empty alias: {registry_path}")
                with open(registry_path, "w"):
                    pass
                continue
            aliases[playlist_id] = alias

        readme_lines = []
        playlist_names_to_ids: Dict[str, Set[PlaylistID]] = collections.defaultdict(set)
        for playlist_id in playlist_ids:
            plain_path = os.path.join(plain_dir, playlist_id)
            pretty_md_path = os.path.join(pretty_dir, f"{playlist_id}.md")
            pretty_json_path = os.path.join(pretty_dir, f"{playlist_id}.json")
            cumulative_md_path = os.path.join(cumulative_dir, f"{playlist_id}.md")

            # Get the data from Spotify
            logger.info(f"Fetching playlist: {playlist_id}")
            playlist = await spotify.get_playlist(playlist_id, aliases)
            logger.info(f"Playlist name: {playlist.name}")
            playlist_names_to_ids[playlist.name].add(playlist_id)
            readme_lines.append(
                "- [{}]({})".format(
                    playlist.name,
                    URL.pretty(playlist_id),
                )
            )

            # Update plain playlist
            prev_content = cls._get_file_content_or_empty_string(plain_path)
            content = Formatter.plain(playlist_id, playlist)
            cls._write_to_file_if_content_changed(
                prev_content=prev_content,
                content=content,
                path=plain_path,
            )

            # Update pretty markdown
            prev_content = cls._get_file_content_or_empty_string(pretty_md_path)
            content = Formatter.pretty(playlist_id, playlist)
            cls._write_to_file_if_content_changed(
                prev_content=prev_content,
                content=content,
                path=pretty_md_path,
            )

            # Update pretty JSON
            prev_content = cls._get_file_content_or_empty_string(pretty_json_path)
            cls._write_to_file_if_content_changed(
                prev_content=prev_content,
                content=json.dumps(
                    dataclasses.asdict(playlist),
                    indent=2,
                    sort_keys=True,
                ),
                path=pretty_json_path,
            )

            # Update cumulative markdown
            prev_content = cls._get_file_content_or_empty_string(cumulative_md_path)
            content = Formatter.cumulative(now, prev_content, playlist_id, playlist)
            cls._write_to_file_if_content_changed(
                prev_content=prev_content,
                content=content,
                path=cumulative_md_path,
            )

        # Check for duplicate playlist names
        duplicate_names = {
            name: playlist_ids
            for name, playlist_ids in playlist_names_to_ids.items()
            if len(playlist_ids) > 1
        }
        if duplicate_names:
            raise Exception(f"Duplicate playlist names: {duplicate_names}")

        # Check for unexpected files in playlist directories
        unexpected_files: Set[str] = set()
        for directory, suffixes in [
            (plain_dir, [""]),
            (pretty_dir, [".md", ".json"]),
            (cumulative_dir, [".md"]),
        ]:
            for filename in os.listdir(directory):
                if not any(
                    cls._remove_suffix(filename, suffix) in playlist_ids
                    for suffix in suffixes
                ):
                    unexpected_files.add(os.path.join(directory, filename))
        if unexpected_files:
            raise Exception(f"Unexpected files: {unexpected_files}")

        # Lastly, update README.md
        if prod:
            readme = open("README.md").read().splitlines()
            index = readme.index("## Playlists")
            lines = (
                readme[: index + 1]
                + [""]
                + sorted(readme_lines, key=lambda line: line.lower())
            )
            with open("README.md", "w") as f:
                f.write("\n".join(lines) + "\n")

    @classmethod
    def _remove_suffix(cls, string: str, suffix: str) -> str:
        if suffix and string.endswith(suffix):
            return string[: -len(suffix)]
        return string

    @classmethod
    def _get_file_content_or_empty_string(cls, path: str) -> str:
        try:
            with open(path, "r") as f:
                return "".join(f.readlines())
        except FileNotFoundError:
            return ""

    @classmethod
    def _write_to_file_if_content_changed(
        cls, prev_content: str, content: str, path: str
    ) -> None:
        if content == prev_content:
            logger.info(f"No changes to file: {path}")
            return
        logger.info(f"Writing updates to file: {path}")
        with open(path, "w") as f:
            f.write(content)
