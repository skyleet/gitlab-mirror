#    Friendly Telegram (telegram userbot)
#    Copyright (C) 2018-2019 The Authors

#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.

#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.

#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

import logging
import os
import sys
import atexit
import argparse
import asyncio
import json
import functools
import collections
import sqlite3
import importlib
import signal
import time
import requests

from telethon import TelegramClient, events
from telethon.sessions import StringSession, SQLiteSession
from telethon.errors.rpcerrorlist import PhoneNumberInvalidError, MessageNotModifiedError, ApiIdInvalidError
from telethon.tl.functions.channels import DeleteChannelRequest

from . import utils, loader, heroku
from .dispatcher import CommandDispatcher


from .database import backend, local_backend, frontend
from .translations.core import Translator

try:
    from .web import core
except ImportError:
    web_available = False
    logging.error("Unable to import web")
else:
    web_available = True


def run_config(db, phone=None, modules=None):
    """Load configurator.py"""
    from . import configurator
    return configurator.run(db, phone, phone is None, modules)


def parse_arguments():
    """Parse the arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", "-s", action="store_true")
    parser.add_argument("--phone", "-p", action="append")
    parser.add_argument("--token", "-t", action="append", dest="tokens")
    parser.add_argument("--heroku", action="store_true")
    parser.add_argument("--local-db", dest="local", action="store_true")
    parser.add_argument("--web-only", dest="web_only", action="store_true")
    parser.add_argument("--no-web", dest="web", action="store_false")
    parser.add_argument("--heroku-web-internal", dest="heroku_web_internal", action="store_true",
                        help="This is for internal use only. If you use it, things will go wrong.")
    parser.add_argument("--heroku-deps-internal", dest="heroku_deps_internal", action="store_true",
                        help="This is for internal use only. If you use it, things will go wrong.")
    parser.add_argument("--heroku-restart-internal", dest="heroku_restart_internal", action="store_true",
                        help="This is for internal use only. If you use it, things will go wrong.")
    arguments = parser.parse_args()
    logging.debug(arguments)
    if sys.platform == "win32":
        # Subprocess support; not needed in 3.8 but not harmful
        asyncio.set_event_loop(asyncio.ProactorEventLoop())

    return arguments


def get_phones(arguments):
    """Get phones from the --token, --phone, and environment"""
    phones = set(arguments.phone if arguments.phone else [])
    phones.update(map(lambda f: f[18:-8],
                      filter(lambda f: f.startswith("friendly-telegram-") and f.endswith(".session"),
                             os.listdir(os.path.dirname(utils.get_base_dir())))))

    authtoken = os.environ.get("authorization_strings", False)  # for heroku
    if authtoken and not arguments.setup:
        try:
            authtoken = json.loads(authtoken)
        except json.decoder.JSONDecodeError:
            logging.warning("authtoken invalid")
            authtoken = False

    if arguments.setup or (arguments.tokens and not authtoken):
        authtoken = {}
    if arguments.tokens:
        for token in arguments.tokens:
            phone = sorted(phones).pop(0)
            phones.remove(phone)  # Handled seperately by authtoken logic
            authtoken.update(**{phone: token})
    return phones, authtoken


def get_api_token():
    """Get API Token from disk or environment"""
    while True:
        try:
            from . import api_token
        except ImportError:
            try:
                api_token = collections.namedtuple("api_token", ("ID", "HASH"))(os.environ["api_id"],
                                                                                os.environ["api_hash"])
            except KeyError:
                return None
        return api_token


def sigterm(app, signum, handler):
    if app is not None:
        dyno = os.environ["DYNO"]
        if dyno.startswith("web"):
            if app.process_formation()["web"].quantity:
                # If we are just idling, start the worker, but otherwise shutdown gracefully
                app.scale_formation_process("worker-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK", 1)
        elif dyno.startswith("restarter"):
            if app.process_formation()["restarter-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK"].quantity:
                # If this dyno is restarting, it means we should start the web dyno
                app.batch_scale_formation_processes({"web": 1, "worker-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK": 0,
                                                     "restarter-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK": 0})
    # This ensures that we call atexit hooks and close FDs when Heroku kills us un-gracefully
    sys.exit(143)  # SIGTERM + 128


def main():  # noqa: C901
    """Main entrypoint"""
    arguments = parse_arguments()
    loop = asyncio.get_event_loop()

    clients = []
    phones, authtoken = get_phones(arguments)
    api_token = get_api_token()

    if web_available:
        web = core.Web(api_token=api_token) if arguments.web else None
    else:
        if arguments.heroku_web_internal:
            raise RuntimeError("Web required but unavailable")
        web = None

    while api_token is None:
        if web:
            loop.run_until_complete(web.start())
            print("Web mode ready for configuration")  # noqa: T001
            if not arguments.heroku_web_internal:
                print("Please visit http://localhost:" + str(web.port))  # noqa: T001
            loop.run_until_complete(web.wait_for_api_token_setup())
            api_token = web.api_token
        else:
            run_config({})
            importlib.invalidate_caches()
            api_token = get_api_token()

    if os.environ.get("authorization_strings", False):
        if os.environ.get("DYNO", False) or arguments.heroku_web_internal or arguments.heroku_deps_internal:
            app, config = heroku.get_app(os.environ["authorization_strings"],
                                         os.environ["heroku_api_token"], api_token, False, True)
        if arguments.heroku_web_internal:
            app.scale_formation_process("worker-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK", 0)
            signal.signal(signal.SIGTERM, functools.partial(sigterm, app))
        elif arguments.heroku_deps_internal:
            try:
                app.scale_formation_process("web", 0)
                app.scale_formation_process("worker-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK", 0)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code != 404:
                    # The dynos don't exist on the very first deployment, so don't try to scale
                    raise
            else:
                atexit.register(functools.partial(app.scale_formation_process,
                                                  "restarter-DO-NOT-TURN-ON-OR-THINGS-WILL-BREAK", 1))
        elif arguments.heroku_restart_internal:
            signal.signal(signal.SIGTERM, functools.partial(sigterm, app))
            while True:
                time.sleep(60)
        elif os.environ.get("DYNO", False):
            signal.signal(signal.SIGTERM, functools.partial(sigterm, app))

    if authtoken:
        for phone, token in authtoken.items():
            try:
                clients += [TelegramClient(StringSession(token), api_token.ID, api_token.HASH,
                                           connection_retries=None).start(phone)]
            except ValueError:
                run_config({})
                return
            clients[-1].phone = phone  # for consistency
    if not clients and not phones:
        if web:
            if not web.running.is_set():
                loop.run_until_complete(web.start())
                print("Web mode ready for configuration")  # noqa: T001
                if not arguments.heroku_web_internal:
                    print("Please visit http://localhost:" + str(web.port))  # noqa: T001
            loop.run_until_complete(web.wait_for_clients_setup())
            arguments.heroku = web.heroku_api_token
            clients = web.clients
            for client in clients:
                if arguments.heroku:
                    session = StringSession()
                else:
                    session = SQLiteSession(os.path.join(os.path.dirname(utils.get_base_dir()),
                                                         "friendly-telegram-" + client.phone))
                session.set_dc(client.session.dc_id, client.session.server_address, client.session.port)
                session.auth_key = client.session.auth_key
                if not arguments.heroku:
                    session.save()
                client.session = session
        else:
            try:
                phones = [input("Please enter your phone or bot token: ")]
            except EOFError:
                print()  # noqa: T001
                print("=" * 30)  # noqa: T001
                print()  # noqa: T001
                print("Hello. If you are seeing this, it means YOU ARE DOING SOMETHING WRONG!")  # noqa: T001
                print()  # noqa: T001
                print("It is likely that you tried to deploy to heroku - "  # noqa: T001
                      "you cannot do this via the web interface.")
                print("To deploy to heroku, go to "  # noqa: T001
                      "https://friendly-telegram.gitlab.io/heroku to learn more")
                print()  # noqa: T001
                print("In addition, you seem to have forked the friendly-telegram repo. THIS IS WRONG!")  # noqa: T001
                print("You should remove the forked repo, and read https://friendly-telegram.gitlab.io")  # noqa: T001
                print()  # noqa: T001
                print("If you're not using Heroku, then you are using a non-interactive prompt but "  # noqa: T001
                      "you have not got a session configured, meaning authentication to Telegram is "
                      "impossible.")  # noqa: T001
                print()  # noqa: T001
                print("THIS ERROR IS YOUR FAULT. DO NOT REPORT IT AS A BUG!")  # noqa: T001
                print("Goodbye.")  # noqa: T001
                sys.exit(1)
    for phone in phones:
        if arguments.heroku:
            session = StringSession()
        else:
            session = os.path.join(os.path.dirname(utils.get_base_dir()),
                                   "friendly-telegram" + (("-" + phone) if phone else ""))
        try:
            client = TelegramClient(session, api_token.ID, api_token.HASH, connection_retries=None).start(phone)
            if ":" in phone:
                client.start(bot_token=phone)
                client.phone = None
                del phone
            else:
                client.start(phone)
                client.phone = phone
            clients.append(client)
        except sqlite3.OperationalError as ex:
            print("Error initialising phone " + (phone if phone else "unknown") + " " + ",".join(ex.args)  # noqa: T001
                  + ": this is probably your fault. Try checking that this is the only instance running and "
                  "that the session is not copied. If that doesn't help, delete the file named '"
                  "friendly-telegram" + (("-" + phone) if phone else "") + ".session'")
            continue
        except (ValueError, ApiIdInvalidError):
            # Bad API hash/ID
            run_config({})
            return
        except PhoneNumberInvalidError:
            print("Please check the phone number. Use international format (+XX...)"  # noqa: T001
                  " and don't put spaces in it.")
            continue
    del phones

    if arguments.heroku:
        if isinstance(arguments.heroku, str):
            key = arguments.heroku
        else:
            key = input("Please enter your Heroku API key (from https://dashboard.heroku.com/account): ").strip()
        app = heroku.publish(clients, key, api_token)
        print("Installed to heroku successfully! Type .help in Telegram for help.")  # noqa: T001
        if web:
            web.redirect_url = app.web_url
            web.ready.set()
            loop.run_until_complete(web.root_redirected.wait())
        return

    loops = [amain(client, clients, web, arguments) for client in clients]

    loop.set_exception_handler(lambda _, x:
                               logging.error("Exception on event loop! %s", x["message"], exc_info=x["exception"]))
    loop.run_until_complete(asyncio.gather(*loops))


async def amain(client, allclients, web, arguments):
    """Entrypoint for async init, run once for each user"""
    setup = arguments.setup
    local = arguments.local
    web_only = arguments.web_only
    async with client:
        client.parse_mode = "HTML"
        await client.start()
        [handler] = logging.getLogger().handlers
        dbc = local_backend.LocalBackend if local else backend.CloudBackend
        if setup:
            db = dbc(client)
            await db.init(lambda e: None)
            jdb = await db.do_download()
            try:
                pdb = json.loads(jdb)
            except (json.decoder.JSONDecodeError, TypeError):
                pdb = {}
            modules = loader.Modules()
            babelfish = Translator([])
            await babelfish.init(client)
            modules.register_all(babelfish)
            fdb = frontend.Database(dbc(client), True)
            await fdb.init()
            modules.send_config(fdb, babelfish)
            await modules.send_ready(client, fdb, allclients)  # Allow normal init even in setup
            handler.setLevel(50)
            pdb = run_config(pdb, getattr(client, "phone", "Unknown Number"), modules)
            if pdb is None:
                await client(DeleteChannelRequest(db.db))
                return
            try:
                await db.do_upload(json.dumps(pdb))
            except MessageNotModifiedError:
                pass
            return
        db = frontend.Database(dbc(client), arguments.heroku_deps_internal)
        await db.init()
        logging.debug("got db")
        logging.info("Loading logging config...")
        handler.setLevel(db.get(__name__, "loglevel", logging.WARNING))

        babelfish = Translator(db.get(__name__, "langpacks", []), db.get(__name__, "language", ["en"]))
        await babelfish.init(client)

        modules = loader.Modules()

        if web and not arguments.heroku_deps_internal:
            await web.add_loader(client, modules, db)
            await web.start_if_ready(len(allclients))

        modules.register_all(babelfish, None if not arguments.heroku_deps_internal else ["loader.py"])

        modules.send_config(db, babelfish)
        await modules.send_ready(client, db, allclients)
        if arguments.heroku_deps_internal:
            # Loader has installed all dependencies
            return  # We are done
        if not web_only:
            dispatcher = CommandDispatcher(modules, db, await client.is_bot())
            await dispatcher.init(client)
            client.add_event_handler(dispatcher.handle_incoming,
                                     events.NewMessage(incoming=True))
            client.add_event_handler(dispatcher.handle_command,
                                     events.NewMessage(forwards=False))
        print("Started for " + str((await client.get_me(True)).user_id))  # noqa: T001
        await client.run_until_disconnected()
