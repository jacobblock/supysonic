# coding: utf-8

# This file is part of Supysonic.
#
# Supysonic is a Python implementation of the Subsonic server API.
# Copyright (C) 2013, 2014  Alban 'spl0k' Féron
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os, os.path
import time
import datetime
from mediafile import MediaFile
import config
import math
import sys, traceback
from web import app

from db import Track, Folder, Artist, Album, Playlist, session

class Scanner:
    def __init__(self, session):
        app.logger.debug('Loading tracks')
        self.__tracks  = session.query(Track.path, Track.id).all()
        self.__tracks = {x.path: x.id for x in self.__tracks}

        app.logger.debug('Loading artists')
        self.__artists = session.query(Artist).all()
        self.__artists = {x.name.lower(): x for x in self.__artists}

        app.logger.debug('Loading folders')
        self.__folders = session.query(Folder).all()
        self.__folders = {x.path: x for x in self.__folders}

        app.logger.debug('Loading playlists')
        self.__playlists = session.query(Playlist).all()

        self.__added_artists = 0
        self.__added_albums  = 0
        self.__added_tracks  = 0
        self.__deleted_artists = 0
        self.__deleted_albums  = 0
        self.__deleted_tracks  = 0

        extensions = config.get('base', 'scanner_extensions')
        self.__extensions = map(str.lower, extensions.split()) if extensions else None

    def scan(self, root_folder):
        print "scanning", root_folder.path
        valid = [x.lower() for x in config.get('base','filetypes').split(',')]
        valid = tuple(valid)
        print "valid filetypes: ",valid

        for root, subfolders, files in os.walk(root_folder.path, topdown=False):

            if root in self.__folders:
                folder = self.__folders[root]
                mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(folder.path))
                app.logger.debug('mtime: %s , last_scan: %s', mod_time, folder.last_scan)
                if mod_time < folder.last_scan:
                    app.logger.debug('Folder not modified, skipping files')
                    continue
                folder.last_scan = datetime.datetime.now()
            else:
                app.logger.debug('Adding folder: ' + root)
                folder = Folder(path = root, parent = root_folder)
                folder.created = datetime.datetime.fromtimestamp(os.path.getctime(root))

                self.__folders[root] = folder

            #TODO: only scan files if folder mtime changed, but is it windows compat?
            # need to see how this works on ntfs-3g
            for f in files:
                if f.lower().endswith(valid):
                    try:
                        path = os.path.join(root, f)
                        scanned_file = self.__scan_file(path)

                        if(scanned_file):
                            scanned_file.folder = folder
                            session.add(scanned_file)
                            session.flush()

                    except:
                        app.logger.error('Problem adding file: ' + os.path.join(root,f))
                        app.logger.error(traceback.print_exc())
                        pass

        root_folder.last_scan = datetime.datetime.now()
        session.flush()
        session.commit()


    def __scan_file(self, path):
        curmtime = int(math.floor(os.path.getmtime(path)))

        if path in self.__tracks:
            tr = session.query(Track).get(self.__tracks[path])

            if curmtime <= tr.last_modification:
                app.logger.debug('\tFile not modified: ' + path)
                return tr

            app.logger.debug('\tFile modified, updating tag')
            app.logger.debug('\tcurmtime %s / last_mod %s', curmtime, tr.last_modification)
            app.logger.debug('\t\t%s Seconds Newer\n\t\t', str(curmtime - tr.last_modification))

            try:
                mf = MediaFile(path)
            except:
                app.logger.error('Problem reading file: ' + path)
                app.logger.error(traceback.print_exc())
                return tr

        else:
            app.logger.debug('Scanning File: ' + path + '\n\tReading tag')

            try:
                mf = MediaFile(path)
            except:
                app.logger.error('Problem reading file: ' + path)
                app.logger.error(traceback.print_exc())
                return False

            tr = Track(path = path)

            self.__added_tracks += 1

        tr.last_modification = curmtime

        # read in file tags
        tr.disc = getattr(mf, 'disc')
        tr.number = getattr(mf, 'track')
        tr.title = getattr(mf, 'title')
        tr.year = getattr(mf, 'year')
        tr.genre = getattr(mf, 'genre')
        tr.artist = getattr(mf, 'artist')
        tr.bitrate  = getattr(mf, 'bitrate')/1000
        tr.duration = getattr(mf, 'length')

        albumartist = getattr(mf, 'albumartist')
        if (albumartist == u''):
            # Use folder name two levels up if no albumartist tag found
            # Assumes structure main -> artist -> album -> song.file
            # That way the songs in compilations will show up in the same album
            albumartist = os.path.basename(os.path.dirname(os.path.dirname(path)))

        tr.created = datetime.datetime.fromtimestamp(curmtime)

        # album year is the same as year of first track found from album, might be inaccurate
        tr.album    = self.__find_album(albumartist, getattr(mf, 'album'), tr.year)

        return tr

    def __find_album(self, artist, album, yr):
        # TODO : DB specific issues with single column name primary key
        #		for instance, case sensitivity and trailing spaces
        artist = artist.rstrip()

        if artist.lower() in self.__artists:
            app.logger.debug('Artist already exists')
            ar = self.__artists[artist.lower()]
        else:
            #Flair!
            sys.stdout.write('\033[K')
            sys.stdout.write('%s\r' % artist.encode('utf-8'))
            sys.stdout.flush()
            ar = Artist(name = artist)
            self.__artists[artist.lower()] = ar
            self.__added_artists += 1

        al = {a.name: a for a in ar.albums}
        if album in al:
            return al[album]
        else:
            self.__added_albums += 1
            album = Album(name = album, artist = ar, year = yr)
            return album

    def prune(self, folder):

        #Should check folders existence instead maybe?
        #Just takes too long to check every file still exists

        for t in self.__tracks.keys():
            if(not os.path.isfile(t)):
                session.delete(session.query(Track).get(self.__tracks[t]))
        session.commit()

        app.logger.debug('Checking for empty albums...')
        for album in session.query(Album).filter(~Album.id.in_(session.query(Track.album_id).distinct())):
            app.logger.debug(album.name + ' Removed')
            album.artist.albums.remove(album)
            session.delete(album)
            self.__deleted_albums += 1
        session.commit()

        app.logger.debug('Checking for artists with no albums...')
        for artist in session.query(Artist).filter(~Artist.id.in_(session.query(Album.artist_id))):
            session.delete(artist)
            self.__deleted_artists += 1
        session.commit()

    def __remove_track(self, track):
        track.album.tracks.remove(track)
        track.folder.tracks.remove(track)
        # As we don't have a track -> playlists relationship, SQLAlchemy doesn't know it has to remove tracks
        # from playlists as well, so let's help it
        for playlist in self.__playlists:
            if track in playlist.tracks:
                playlist.tracks.remove(track)

        session.delete(track)
        self.__deleted_tracks += 1

    def stats(self):
        return (self.__added_artists, self.__added_albums, self.__added_tracks), (self.__deleted_artists, self.__deleted_albums, self.__deleted_tracks)

