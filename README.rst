===================================================
pytusk - Walrus and SEAL SDK (ALPHA)
===================================================

A python SDK for building interactions with Walrus storage management. 

Installing ``pytusk`` automatically installes ``pysui`` SDK for underlying configuration and transport support.

*****************
Installation
*****************

1. Create a virtual environment (e.g. ``venv``, ``pipenv``, etc.)
2. Enter the virtual environment
3. Install ``pytusk`` (dependent on what virtual environment being used)


*****************
Quick start
*****************

-------------------
Configuration setup
-------------------

As noted, ``pytusk`` leverages ``pysui`` for configuration and client transport support. So 
when setting up to interact with Walrus the configuration setup is first step::

    import asyncio
    from pysui import PysuiConfiguration
    from pytusk.config.tusk_config import PytuskConfiguration
    from pytusk.clients.walrus_client import ClientAsync

    async def main(client:ClientAsync):
        """Work with Walrus"""

    # pylint: disable=broad-exception-caught

    if __name__ == "__main__":

        client_init: ClientAsync = None
        try:
            cfg = PytuskConfiguration(
                pysui_config=PysuiConfiguration(
                    group_name=PysuiConfiguration.SUI_JSON_RPC_GROUP,
                    profile_name="testnet",
                ),
                walrus_aggregator="https://aggregator.walrus-testnet.walrus.space",
                walrus_publisher="https://publisher.walrus-testnet.walrus.space",
            )
            client_init = ClientAsync(pytusk_config=cfg)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(main(client_init))
            except KeyboardInterrupt:
                pass

        except Exception as ex:
            print(ex.args)

