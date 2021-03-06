# coding: utf-8

# This file is part of Supysonic.
#
# Supysonic is a Python implementation of the Subsonic server API.
# Copyright (C) 2013  Alban 'spl0k' Féron
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

import traceback
import sys
from flask import request, send_file, Response
import requests
import os.path
from PIL import Image
from StringIO import StringIO
import subprocess
import codecs
from xml.etree import ElementTree
import ushlex as shlex
import fnmatch
import mimetypes
from mediafile import MediaFile
import mutagen

import config
from web import app
from db import Track, Album, Artist, Folder, ClientPrefs, now, session
from . import get_entity

from sqlalchemy import func

from flask import g


def after_this_request(func):
    if not hasattr(g, 'call_after_request'):
        g.call_after_request = []
    g.call_after_request.append(func)
    return func


@app.after_request
def per_request_callbacks(response):
    for f in getattr(g, 'call_after_request', ()):
        response = f(response)
    return response


def prepare_transcoding_cmdline(base_cmdline, input_file,
                                input_format, output_format, output_bitrate):
    if not base_cmdline:
        return None

    return base_cmdline\
        .replace('%srcpath', '"'+input_file+'"')\
        .replace('%srcfmt', input_format)\
        .replace('%outfmt', output_format)\
        .replace('%outrate', str(output_bitrate))


@app.route('/rest/stream.view', methods=['GET', 'POST'])
def stream_media():

    @after_this_request
    def add_header(resp):
        if 'X-Sendfile' in resp.headers:
            app.logger.debug('Using X-Sendfile or X-Accel-Redirect')
            resp.headers['X-Accel-Charset'] = 'utf-8'
            resp.headers['X-Accel-Redirect'] = resp.headers['X-Sendfile']

        return resp

    def transcode(process):
        try:
            for chunk in iter(process.stdout.readline, ''):
                yield chunk
            process.wait()
        except:
            traceback.print_exc()
            process.terminate()
            process.wait()

    status, res = get_entity(request, Track)

    if not status:
        return res

    maxBitRate, format, timeOffset, size, estimateContentLength, client = map(request.args.get, [ 'maxBitRate', 'format', 'timeOffset', 'size', 'estimateContentLength', 'c' ])
    if format:
        format = format.lower()

    do_transcoding = False
    src_suffix = res.suffix()
    dst_suffix = src_suffix
    dst_bitrate = res.bitrate
    dst_mimetype = mimetypes.guess_type('a.' + src_suffix)

    if maxBitRate:
        try:
            maxBitRate = int(maxBitRate)
        except:
            return request.error_formatter(0, 'Invalid bitrate value')

        if dst_bitrate > maxBitRate and maxBitRate != 0:
            do_transcoding = True
            dst_bitrate = maxBitRate

    if format and format != 'raw' and format != src_suffix:
        do_transcoding = True
        dst_suffix = format
        dst_mimetype = mimetypes.guess_type(dst_suffix)

    if client:
        prefs = session.query(ClientPrefs).get((request.user.id, client))
        if not prefs:
            prefs = ClientPrefs(user_id = request.user.id, client_name = client)
            session.add(prefs)

        if prefs.format:
            dst_suffix = prefs.format
        if prefs.bitrate and prefs.bitrate < dst_bitrate:
            dst_bitrate = prefs.bitrate


    if not format and src_suffix == 'flac':
        dst_suffix = 'ogg'
        dst_bitrate = 320
        dst_mimetype = 'audio/ogg'
        do_transcoding = True

    duration = mutagen.File(res.path).info.length

    if do_transcoding:
        transcoder = config.get('transcoding', 'transcoder_{}_{}'.format(src_suffix, dst_suffix))

        decoder = config.get('transcoding', 'decoder_' + src_suffix) or config.get('transcoding', 'decoder')
        encoder = config.get('transcoding', 'encoder_' + dst_suffix) or config.get('transcoding', 'encoder')

        if not transcoder and (not decoder or not encoder):
            transcoder = config.get('transcoding', 'transcoder')
            if not transcoder:
                return request.error_formatter(0, 'No way to transcode from {} to {}'.format(src_suffix, dst_suffix))

        transcoder, decoder, encoder = map(lambda x: prepare_transcoding_cmdline(x, res.path, src_suffix, dst_suffix, dst_bitrate), [ transcoder, decoder, encoder ])

        if '|' in transcoder:
            pipe_index = transcoder.index('|')
            decoder = transcoder[:pipe_index]
            encoder = transcoder[pipe_index+1:]
            transcoder = None

        try:
            if not transcoder:
                decoder = map(lambda s: s.decode('UTF8'), shlex.split(decoder.encode('utf8')))
                encoder = map(lambda s: s.decode('UTF8'), shlex.split(encoder.encode('utf8')))
                dec_proc = subprocess.Popen(decoder, stdout = subprocess.PIPE, shell=False)
                proc = subprocess.Popen(encoder, stdin=dec_proc.stdout, stdout=subprocess.PIPE, shell=False)
            else:
                transcoder = map(lambda s: s.decode('UTF8'), shlex.split(transcoder.encode('utf8')))
                proc = subprocess.Popen(transcoder, stdout = subprocess.PIPE, shell=False)

            response = Response(transcode(proc), 200, {'Content-Type': dst_mimetype, 'X-Content-Duration': str(duration)})
        except:
            traceback.print_exc()
            return request.error_formatter(0, 'Error while running the transcoding process: {}'.format(sys.exc_info()[1]))

    else:
        response = send_file(res.path.encode('utf-8'))
        response.headers['Content-Type'] = dst_mimetype
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['X-Content-Duration'] = str(duration)

    res.play_count = res.play_count + 1
    res.last_play = now()
    request.user.last_play = res
    request.user.last_play_date = now()
    session.commit()

    return response


@app.route('/rest/download.view', methods = [ 'GET', 'POST' ])
def download_media():
    status, res = get_entity(request, Track)
    if not status:
        return res

    return send_file(res.path)


@app.route('/rest/getCoverArt.view', methods = [ 'GET', 'POST' ])
def cover_art():

    @after_this_request
    def add_header(resp):
        if 'X-Sendfile' in resp.headers:
            app.logger.debug('Using X-Sendfile or X-Accel-Redirect')
            app.logger.debug(resp.headers['X-Sendfile'])
            resp.headers['X-Accel-Redirect'] = resp.headers['X-Sendfile']
        return resp

    # retrieve folder from database
    status, res = get_entity(request, Folder)

    if not status:
        return res

    # Check the folder id given for jpgs
    app.logger.debug('Cover Art Check: ' + res.path + '/*.jp*g')

    coverfile = os.listdir(res.path)
    coverfile = fnmatch.filter(coverfile, '*.jp*g')

    # when there is not a jpeg in the folder check files for embedded art
    if not coverfile:
        app.logger.debug('No Art Found in Folder, Checking Files!')

        for tr in res.tracks:
            app.logger.debug('Checking ' + tr.path + ' For Artwork')

            try:
                mf = MediaFile(tr.path)
                coverfile = getattr(mf, 'art')

                if coverfile is not None:
                    if type(coverfile) is list:
                        coverfile = coverfile[0]
                    coverfile = StringIO(coverfile)
                    app.logger.debug('Serving embedded cover art')
                    break

            except:
                app.logger.debug('Problem reading embedded art')
                return request.error_formatter(70, 'Cover art not found'), 404

            return request.error_formatter(70, 'Cover art not found'), 404
    else:
        app.logger.debug('Found Images: ' + str(coverfile))
        coverfile = coverfile[0]
        coverfile = os.path.join(res.path, coverfile)
        app.logger.debug('Serving cover art: ' + coverfile)

    size = request.args.get('size')
    if size:
        try:
            size = int(size)
        except:
            return request.error_formatter(0, 'Invalid size value'), 500
    else:
        size = 1000

    im = Image.open(coverfile)

    size_path = os.path.join(config.get('base', 'cache_dir'), str(size))
    path = os.path.join(size_path, str(res.id))

    if not os.path.exists(size_path):
        os.makedirs(size_path)

    if size > im.size[0] and size > im.size[1]:
        app.logger.debug('Not resizing Image, adding to cache')
        im.save(path, 'JPEG')
        return send_file(path)

    app.logger.debug('Saving resized image to: ' + path)

    if os.path.exists(path):
        app.logger.debug('Serving cover art: ' + path)
        return send_file(path)


    im.thumbnail([size, size], Image.ANTIALIAS)
    im.save(path, 'JPEG')

    app.logger.debug('Serving cover art: ' + path)
    return send_file(path)

@app.route('/rest/getLyrics.view', methods = [ 'GET', 'POST' ])
def lyrics():
    artist, title = map(request.args.get, [ 'artist', 'title' ])
    if not artist:
        return request.error_formatter(10, 'Missing artist parameter')
    if not title:
        return request.error_formatter(10, 'Missing title parameter')

    query = session.query(Track).join(Album, Artist).filter(func.lower(Track.title) == title.lower() and func.lower(Artist.name) == artist.lower())
    for track in query:
        lyrics_path = os.path.splitext(track.path)[0] + '.txt'
        if os.path.exists(lyrics_path):
            app.logger.debug('Found lyrics file: ' + lyrics_path)

            try:
                lyrics = read_file_as_unicode(lyrics_path)
            except UnicodeError:
                # Lyrics file couldn't be decoded. Rather than displaying an error, try with the potential next files or
                # return no lyrics. Log it anyway.
                app.logger.warn('Unsupported encoding for lyrics file ' + lyrics_path)
                continue

            return request.formatter({ 'lyrics': {
                'artist': track.album.artist.name,
                'title': track.title,
                '_value_': lyrics
            } })

    try:
        r = requests.get("http://api.chartlyrics.com/apiv1.asmx/SearchLyricDirect",
                         params = { 'artist': artist, 'song': title })
        root = ElementTree.fromstring(r.content)

        ns = { 'cl': 'http://api.chartlyrics.com/' }
        return request.formatter({ 'lyrics': {
            'artist': root.find('cl:LyricArtist', namespaces = ns).text,
            'title': root.find('cl:LyricSong', namespaces = ns).text,
            '_value_': root.find('cl:Lyric', namespaces = ns).text
        } })
    except requests.exceptions.RequestException, e:
        app.logger.warn('Error while requesting the ChartLyrics API: ' + str(e))

    return request.formatter({ 'lyrics': {} })

def read_file_as_unicode(path):
    """ Opens a file trying with different encodings and returns the contents as a unicode string """

    encodings = [ 'utf-8', 'latin1' ] # Should be extended to support more encodings

    for enc in encodings:
        try:
            contents = codecs.open(path, 'r', encoding = enc).read()
            app.logger.debug('Read file {} with {} encoding'.format(path, enc))
            # Maybe save the encoding somewhere to prevent going through this loop each time for the same file
            return contents
        except UnicodeError:
            pass

    # Fallback to ASCII
    app.logger.debug('Reading file {} with ascii encoding'.format(path))
    return unicode(open(path, 'r').read())

