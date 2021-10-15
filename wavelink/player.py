"""MIT License

Copyright (c) 2019-2021 PythonistaGuild

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import datetime
import logging
from typing import Any, Dict, Union, Optional

import discord
from discord.channel import VoiceChannel

from . import abc
from .pool import Node, NodePool
from .queue import WaitQueue
from .tracks import PartialTrack
from .utils import MISSING


__all__ = ("Player",)


logger: logging.Logger = logging.getLogger(__name__)


VoiceChannel = Union[
    discord.VoiceChannel, discord.StageChannel
]  # todo: VocalGuildChannel?


class Player(discord.VoiceProtocol):
    """WaveLink Player object.

    This class subclasses :class:`discord.VoiceProtocol` and such should be treated as one with additions.

    Examples
    --------

        .. code::

            @commands.command()
            async def connect(self, channel: discord.VoiceChannel):

                voice_client = await channel.connect(cls=wavelink.Player)


    .. warning::
        This class should not be created manually but can be subclassed to add additional functionality.
        You should instead use :meth:`discord.VoiceChannel.connect()` and pass the player object to the cls kwarg.
    """

    def __call__(self, client: discord.Client, channel: VoiceChannel):
        self.client: discord.Client = client
        self.channel: VoiceChannel = channel

        return self

    def __init__(
        self,
        client: discord.Client = MISSING,
        channel: VoiceChannel = MISSING,
        *,
        node: Node = MISSING,
    ):
        self.client: discord.Client = client
        self.channel: VoiceChannel = channel

        if node is MISSING:
            node = NodePool.get_node()
        self.node: Node = node
        self.node._players.append(self)

        self._voice_state: Dict[str, Any] = {}

        self.last_update: datetime.datetime = MISSING
        self.last_position: float = MISSING

        self.volume: float = 100
        self._paused: bool = False
        self._source: Optional[abc.Playable] = None
        # self._equalizer = Equalizer.flat()

        self.queue = WaitQueue()

    @property
    def guild(self) -> discord.Guild:
        """The :class:`discord.Guild` this :class:`Player` is in."""
        return self.channel.guild

    @property
    def user(self) -> discord.ClientUser:
        """The :class:`discord.ClientUser` of the :class:`discord.Client`"""
        return self.client.user  # type: ignore

    @property
    def source(self) -> Optional[abc.Playable]:
        """The currently playing audio source."""
        return self._source

    track = source

    @property
    def position(self) -> float:
        """The current seek position of the playing source in seconds. If nothing is playing this defaults to ``0``."""
        if not self.is_playing():
            return 0

        if self.is_paused():
            return min(self.last_position, self.source.duration)  # type: ignore

        delta = (
            datetime.datetime.now(datetime.timezone.utc) - self.last_update
        ).total_seconds()
        position = round(self.last_position + delta, 1)

        return min(position, self.source.duration)  # type: ignore

    async def update_state(self, state: Dict[str, Any]) -> None:
        state = state["state"]

        self.last_update = datetime.datetime.fromtimestamp(
            state.get("time", 0) / 1000, datetime.timezone.utc
        )
        self.last_position = round(state.get("position", 0) / 1000, 1)

    async def on_voice_server_update(self, data: Dict[str, Any]) -> None:
        self._voice_state.update({"event": data})

        await self._dispatch_voice_update(self._voice_state)

    async def on_voice_state_update(self, data: Dict[str, Any]) -> None:
        self._voice_state.update({"sessionId": data["session_id"]})

        channel_id = data["channel_id"]
        if not channel_id:  # We're disconnecting
            self._voice_state.clear()
            return

        self.channel = self.guild.get_channel(int(channel_id))  # type: ignore
        await self._dispatch_voice_update({**self._voice_state, "event": data})

    async def _dispatch_voice_update(self, voice_state: Dict[str, Any]) -> None:
        logger.debug(f"Dispatching voice update:: {self.channel.id}")

        if {"sessionId", "event"} == self._voice_state.keys():
            await self.node._websocket.send(
                op="voiceUpdate", guildId=str(self.guild.id), **voice_state
            )

    async def connect(self, *, timeout: float, reconnect: bool) -> None:
        await self.guild.change_voice_state(channel=self.channel)
        self._connected = True

        logger.info(f"Connected to voice channel:: {self.channel.id}")

    async def disconnect(self, *, force: bool) -> None:
        try:
            logger.info(f"Disconnected from voice channel:: {self.channel.id}")

            await self.guild.change_voice_state(channel=None)
            self._connected = False
        finally:
            self.node.players.remove(self)
            self.cleanup()

    async def move_to(self, channel: discord.VoiceChannel) -> None:
        """|coro|

        Moves the player to a different voice channel.

        Parameters
        -----------
        channel: :class:`discord.VoiceChannel`
            The channel to move to. Must be a voice channel.
        """
        await self.guild.change_voice_state(channel=channel)
        logger.info(f"Moving to voice channel:: {channel.id}")

    async def play(
        self, source: abc.Playable, replace: bool = True, start: int = 0, end: int = 0
    ):
        """|coro|

        Play a WaveLink Track.

        Parameters
        ----------
        source: :class:`abc.Playable`
            The :class:`abc.Playable` to initiate playing.
        replace: bool
            Whether or not the current track, if there is one, should be replaced or not. Defaults to ``True``.
        start: int
            The position to start the player from in milliseconds. Defaults to ``0``.
        end: int
            The position to end the track on in milliseconds.
            By default this always allows the current song to finish playing.

        Returns
        -------
        :class:`wavelink.abc.Playable`
            The track that is now playing.
        """
        if replace or not self.is_playing():
            await self.update_state({"state": {}})
            self._paused = False
        else:
            return

        if isinstance(source, PartialTrack):
            source = await source._search()

        self._source = source

        payload = {
            "op": "play",
            "guildId": str(self.guild.id),
            "track": source.id,
            "noReplace": not replace,
            "startTime": str(start),
        }
        if end > 0:
            payload["endTime"] = str(end)

        await self.node._websocket.send(**payload)

        logger.debug(f"Started playing track:: {str(source)} ({self.channel.id})")
        return source

    def is_connected(self) -> bool:
        """Indicates whether the player is connected to voice."""
        return self._connected

    def is_playing(self) -> bool:
        """Indicates wether a track is currently being played."""
        return self.is_connected() and self._source is not None

    def is_paused(self) -> bool:
        """Indicates wether the currently playing track is paused."""
        return self._paused

    async def stop(self) -> None:
        """|coro|

        Stop the Player's currently playing song.
        """
        await self.node._websocket.send(op="stop", guildId=str(self.guild.id))
        logger.debug(f"Current track stopped:: {str(self.source)} ({self.channel.id})")
        self._source = None

    async def set_pause(self, pause: bool) -> None:
        """|coro|

        Set the players paused state.

        Parameters
        ----------
        pause: bool
            A bool indicating if the player's paused state should be set to True or False.
        """
        await self.node._websocket.send(
            op="pause", guildId=str(self.guild.id), pause=pause
        )
        self._paused = pause
        logger.info(f"Set pause:: {self._paused} ({self.channel.id})")

    async def pause(self) -> None:
        """|coro|

        Pauses the player if it was playing.
        """
        await self.set_pause(True)

    async def resume(self) -> None:
        """|coro|

        Resumes the player if it was paused.
        """
        await self.set_pause(False)

    async def set_volume(self, volume: int) -> None:
        """|coro|

        Set the player's volume, between 0 and 1000.

        Parameters
        ----------
        volume: int
            The volume to set the player to.
        """
        self.volume = max(min(volume, 1000), 0)
        await self.node._websocket.send(
            op="volume", guildId=str(self.guild.id), volume=self.volume
        )
        logger.debug(f"Set volume:: {self.volume} ({self.channel.id})")

    async def seek(self, position: int = 0) -> None:
        """|coro|

        Seek to the given position in the song.

        Parameters
        ----------
        position: int
            The position as an int in milliseconds to seek to. Could be None to seek to beginning.
        """
        await self.node._websocket.send(
            op="seek", guildId=str(self.guild.id), position=position
        )

    async def set_filters(self, equalizerjson: Union[dict, list] = None, karaokejson: dict = None, timescalejson: dict = None, tremolojson: dict = None, vibratojson: dict = None, rotationjson: Union[dict, float] =None, distortionjson: dict = None, channelMixjson: dict = None, lowPassjson: Union[dict, float]= None) -> None:
        """|coro|

        Set filters, basically equalizer and the other customizations to the song but bundled in one requst

        Parameters
        ----------
        equalizerjson: Union[dict, list]
            Takes in band 0-14, each band has a gain limit from -0.25 to 1.0 (default is 0)
            Example:{'equalizer': [{'band': 0, 'gain': -0.25}, {'band': 1, 'gain': -0.25}, {'band': 2, 'gain': -0.125},
                {'band': 3, 'gain': 0.0}, {'band': 4, 'gain': 0.25}, {'band': 5, 'gain': 0.25}, {'band': 6, 'gain': 0.0},
                {'band': 7, 'gain': -0.25}, {'band': 8, 'gain': -0.25}, {'band': 9, 'gain': 0.0}, {'band': 10, 'gain': 0.0},
                {'band': 11, 'gain': 0.5}, {'band': 12, 'gain': 0.25}, {'band': 13, 'gain': -0.025}]}

            Example parameter:[(0, -0.25), (1, -0.25), (2, -0.125), (3, 0.0),
                  (4, 0.25), (5, 0.25), (6, 0.0), (7, -0.25), (8, -0.25),
                  (9, 0.0), (10, 0.0), (11, 0.5), (12, 0.25), (13, -0.025)]

        karaokejson: dict
            Uses equalization to eliminate part of a band, usually targeting vocals.
            Example: "karaoke": {
                    "level": 1.0,
                    "monoLevel": 1.0,
                    "filterBand": 220.0,
                    "filterWidth": 100.0
                    },

        timescalejson: dict
            Changes the speed, pitch, and rate. All default to 1.
            "timescale": {
            "speed": 1.0,
            "pitch": 1.0,
            "rate": 1.0
            },

        tremolojson: dict
            Uses amplification to create a shuddering effect, where the volume quickly oscillates.
            Example: https://en.wikipedia.org/wiki/File:Fuse_Electronics_Tremolo_MK-III_Quick_Demo.ogv
            Example: "tremolo": {
                    "frequency": 2.0, # 0 < x
                    "depth": 0.5      # 0 < x ≤ 1
                    },

        vibratojson: dict
            Similar to tremolo. While tremolo oscillates the volume, vibrato oscillates the pitch.
            Example: "vibrato": {
                    "frequency": 2.0, # 0 < x ≤ 14
                    "depth": 0.5      # 0 < x ≤ 1
                    },

        rotationjson: Union[dict, float]
            Rotates the sound around the stereo channels/user headphones aka Audio Panning. It can produce an effect similar to: https://youtu.be/QB9EB8mTKcc (without the reverb)
            "rotation": {
            "rotationHz": 0 // The frequency of the audio rotating around the listener in Hz. 0.2 is similar to the example video above.
            },
            Or you can pass a float value in bigger than 0
            Example Parameter: 0.2

        distortionjson: dict
            Distortion effect. It can generate some pretty unique audio effects.
            Example: "distortion": {
                    "sinOffset": 0,
                    "sinScale": 1,
                    "cosOffset": 0,
                    "cosScale": 1,
                    "tanOffset": 0,
                    "tanScale": 1,
                    "offset": 0,
                    "scale": 1
                    }

        channelMixjson: dict
            Mixes both channels (left and right), with a configurable factor on how much each channel affects the other.
            With the defaults, both channels are kept independent from each other.
            Setting all factors to 0.5 means both channels get the same audio.
            Example: "channelMix": {
                    "leftToLeft": 1.0,
                    "leftToRight": 0.0,
                    "rightToLeft": 0.0,
                    "rightToRight": 1.0,
                    }

        lowPassjson: Union[dict, float]
            Higher frequencies get suppressed, while lower frequencies pass through this filter, thus the name low pass.
            "lowPass": {
                        "smoothing": 20.0
                        }
            Or you can pass a float above 0
        """
        if type(equalizerjson) is list:
            levels = equalizerjson
            equalizerjson = {"equalizer":[{"band": i[0], "gain": i[1]} for i in levels]}
        if type(rotationjson) is float:
            rotationjson = {"rotation": {"rotationHz": rotationjson}}
        if type(lowPassjson)is float:
            lowPassjson = {"lowPass":{"smoothing":lowPassjson}}


        payload = {
            "op": "filters",
            "guildId": str(self.guild.id)
        }

        if equalizerjson is not None:
            if type(equalizerjson["equalizer"]) is not list:
                raise Exception("Deformed equalizerjson")
            payload.update(equalizerjson)
        if karaokejson is not None:
            if type(karaokejson["karaoke"]["level"]) is not float or type(karaokejson["karaoke"]["monoLevel"]) is not float or type(karaokejson["karaoke"]["filterBand"]) is not float or type(karaokejson["karaoke"]["filterWidth"]) is not float:
                raise Exception("Deformed karaokejson")
            payload.update(karaokejson)
        if timescalejson is not None:
            if type(timescalejson["timescale"]["speed"]) is not float or type(timescalejson["timescale"]["pitch"]) is not float or type(timescalejson["timescale"]["rate"]) is not float:
                raise Exception("Deformed timescalejson")
            payload.update(timescalejson)
        if tremolojson is not None:
            if type(tremolojson["tremolo"]["frequency"]) is not float or type(tremolojson["tremolo"]["depth"]) is not float:
                raise Exception("Deformed tremolojson")
            payload.update(tremolojson)
        if vibratojson is not None:
            if type(vibratojson["vibrato"]["frequency"]) is not float or type(vibratojson["vibrato"]["depth"]) is not float:
                raise Exception("Deformed vibratojson")
            payload.update(vibratojson)
        if rotationjson is not None:
            if type(rotationjson["rotation"]["rotationHz"]) is not float:
                raise Exception("Deformed rotationjson")
            payload.update(rotationjson)
        if distortionjson is not None:
            if type(distortionjson["distortion"]["sinOffset"]) is not float or type(distortionjson["distortion"]["sinScale"]) is not float or type(distortionjson["distortion"]["cosOffset"]) is not float or type(distortionjson["distortion"]["cosScale"]) is not float or type(distortionjson["distortion"]["tanOffset"]) is not float or type(distortionjson["distortion"]["tanScale"]) is not float or type(distortionjson["distortion"]["offset"]) is not float or type(distortionjson["distortion"]["scale"]) is not float:
                raise Exception("Deformed distortionjson")
            payload.update(distortionjson)
        if channelMixjson is not None:
            if type(channelMixjson["channelMix"]["leftToLeft"]) is not float or type(channelMixjson["channelMix"]["leftToRight"]) is not float or type(channelMixjson["channelMix"]["rightToLeft"]) is not float or type(channelMixjson["channelMix"]["rightToRight"]) is not float:
                raise Exception("Deformed channelMix")
            payload.update(channelMixjson)
        if lowPassjson is not None:
            if type(lowPassjson["lowPass"]["smoothing"]) is not float:
                raise Exception("Deformed lowPassjson")
            payload.update(lowPassjson)

        await self.node._websocket.send(**payload)

    async def set_equalizer(self, equalizerjson: Union[dict, list]) -> None:
        """|coro|

        Takes in band 0-14, each band has a gain limit from -0.25 to 1.0 (default is 0)

        Parameters
        ----------
        equalizerjson: Union[dict, list]
            Takes in band 0-14, each band has a gain limit from -0.25 to 1.0 (default is 0)
            Example:{'equalizer': [{'band': 0, 'gain': -0.25}, {'band': 1, 'gain': -0.25}, {'band': 2, 'gain': -0.125},
                {'band': 3, 'gain': 0.0}, {'band': 4, 'gain': 0.25}, {'band': 5, 'gain': 0.25}, {'band': 6, 'gain': 0.0},
                {'band': 7, 'gain': -0.25}, {'band': 8, 'gain': -0.25}, {'band': 9, 'gain': 0.0}, {'band': 10, 'gain': 0.0},
                {'band': 11, 'gain': 0.5}, {'band': 12, 'gain': 0.25}, {'band': 13, 'gain': -0.025}]}

            Example parameter:[(0, -0.25), (1, -0.25), (2, -0.125), (3, 0.0),
                  (4, 0.25), (5, 0.25), (6, 0.0), (7, -0.25), (8, -0.25),
                  (9, 0.0), (10, 0.0), (11, 0.5), (12, 0.25), (13, -0.025)]

        """


        await self.set_filters(equalizerjson=equalizerjson)
        # payload = {
        #     "op": "filters",
        #     "guildId": str(self.guild.id),
        #     "equalizer":[{"band": i[0], "gain": i[1]} for i in levels]
        # }
        #
        # await self.node._websocket.send(**payload)

    async def set_karaoke(self, karaokejson: dict) -> None:
        """|coro|

        Uses equalization to eliminate part of a band, usually targeting vocals.

        Parameters
        ----------
        karaokejson: dict
            Uses equalization to eliminate part of a band, usually targeting vocals.
            Example: "karaoke": {
                    "level": 1.0,
                    "monoLevel": 1.0,
                    "filterBand": 220.0,
                    "filterWidth": 100.0
                    },

        """

        await self.set_filters(karaokejson=karaokejson)

    async def set_timescale(self, timescalejson: dict) -> None:
        """|coro|

        Changes the speed, pitch, and rate. All default to 1.

        Parameters
        ----------
        timescalejson: dict
            Changes the speed, pitch, and rate. All default to 1.
            "timescale": {
            "speed": 1.0,
            "pitch": 1.0,
            "rate": 1.0
            },

        """

        await self.set_filters(timescalejson=timescalejson)

    async def set_tremolo(self, tremolojson: dict) -> None:
        """|coro|

        Uses amplification to create a shuddering effect, where the volume quickly oscillates.

        Parameters
        ----------
        tremolojson: dict
            Uses amplification to create a shuddering effect, where the volume quickly oscillates.
            Example: https://en.wikipedia.org/wiki/File:Fuse_Electronics_Tremolo_MK-III_Quick_Demo.ogv
            Example: "tremolo": {
                    "frequency": 2.0, # 0 < x
                    "depth": 0.5      # 0 < x ≤ 1
                    },

        """

        await self.set_filters(tremolojson=tremolojson)

    async def set_vibrato(self, vibratojson: dict) -> None:
        """|coro|

        Similar to tremolo. While tremolo oscillates the volume, vibrato oscillates the pitch.

        Parameters
        ----------
        vibratojson: dict
            Similar to tremolo. While tremolo oscillates the volume, vibrato oscillates the pitch.
            Example: "vibrato": {
                    "frequency": 2.0, # 0 < x ≤ 14
                    "depth": 0.5      # 0 < x ≤ 1
                    },

        """

        await self.set_filters(vibratojson=vibratojson)

    async def set_rotation(self, rotationjson: Union[dict, float]) -> None:
        """|coro|

        Rotates the sound around the stereo channels/user headphones aka Audio Panning. It can produce an effect similar to: https://youtu.be/QB9EB8mTKcc (without the reverb)

        Parameters
        ----------
        rotationjson: Union[dict, float]
            Rotates the sound around the stereo channels/user headphones aka Audio Panning. It can produce an effect similar to: https://youtu.be/QB9EB8mTKcc (without the reverb)
            "rotation": {
            "rotationHz": 0 // The frequency of the audio rotating around the listener in Hz. 0.2 is similar to the example video above.
            },
            Or you can pass a float value in bigger than 0
            Example Parameter: 0.2

        """

        await self.set_filters(rotationjson=rotationjson)

    async def set_distortion(self, distortionjson: dict) -> None:
        """|coro|

        Distortion effect. It can generate some pretty unique audio effects.

        Parameters
        ----------
        distortionjson: dict
            Distortion effect. It can generate some pretty unique audio effects.
            Example: "distortion": {
                    "sinOffset": 0,
                    "sinScale": 1,
                    "cosOffset": 0,
                    "cosScale": 1,
                    "tanOffset": 0,
                    "tanScale": 1,
                    "offset": 0,
                    "scale": 1
                    }

        """

        await self.set_filters(distortionjson=distortionjson)

    async def set_channelMix(self, channelMixjson: dict) -> None:
        """|coro|

        Mixes both channels (left and right), with a configurable factor on how much each channel affects the other.
        With the defaults, both channels are kept independent from each other.
        Setting all factors to 0.5 means both channels get the same audio.

        Parameters
        ----------
        channelMixjson: dict
            Mixes both channels (left and right), with a configurable factor on how much each channel affects the other.
            With the defaults, both channels are kept independent from each other.
            Setting all factors to 0.5 means both channels get the same audio.
            Example: "channelMix": {
                    "leftToLeft": 1.0,
                    "leftToRight": 0.0,
                    "rightToLeft": 0.0,
                    "rightToRight": 1.0,
                    }

        """

        await self.set_filters(channelMixjson=channelMixjson)

    async def set_lowPass(self, lowPassjson: Union[dict, float]) -> None:
        """|coro|

        Higher frequencies get suppressed, while lower frequencies pass through this filter, thus the name low pass.

        Parameters
        ----------
        lowPassjson: Union[dict, float]
            Higher frequencies get suppressed, while lower frequencies pass through this filter, thus the name low pass.
            "lowPass": {
                        "smoothing": 20.0
                        }
            Or you can pass a float above 0

        """

        await self.set_filters(lowPassjson=lowPassjson)



