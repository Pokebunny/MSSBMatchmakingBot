# file: MSSBMatchmakingBot.py
# author: Nick Taber / Pokebunny
# version: 8/12/22

import characters

import os
import time
import logging
from json import JSONDecodeError

import requests

import discord
from discord import ButtonStyle
from discord.ui import Button, View
from discord.ext import commands, tasks
from dotenv import load_dotenv

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# load .env file which has discord token
load_dotenv()
TOKEN = os.getenv("MMBOT_TOKEN")
intents = discord.Intents.all()

# initialize the bot commands with the associated prefix
bot = commands.Bot(command_prefix="%", intents=intents, case_insensitive=True)

# use creds to create a client to interact with the Google Drive API
scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("client_secret.json", scope)
client = gspread.authorize(creds)


# Access spreadsheet and store data
stars_off_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-OFF")
stars_on_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-ON")
off_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-OFF")
on_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-ON")
# Create a list of all player ratings (to be used for defining percentile search ranges)
off_rating_list = sorted(list(map(int, stars_off_sheet.col_values(5)[1:])), reverse=True)
on_rating_list = sorted(list(map(int, stars_on_sheet.col_values(5)[1:])), reverse=True)

# Constant for starting percentile range for matchmaking search
PERCENTILE_RANGE = 0.15
# Constant to tell the bot where the matchmaking buttons appear
BUTTON_CHANNEL_ID = 971164238888468520
# Prod: 841761307245281320
# Test: 971164238888468520

# Constant to tell the bot where to post matchmaking updates
MATCH_CHANNEL_ID = 971164132063727636
# Prod: 948321928760918087
# Test: 971164132063727636

# The matchmaking queue
queue = {}
# The message with the matchmaking bot stuff
mm_message = None

mode_list = ["Superstars-Off Ranked", "Superstars-Off Unranked", "Superstars-On Ranked"]

# Initialize logging
logging.basicConfig(filename="match_log.txt", level=logging.INFO)
match_count = 1


@bot.event
async def on_ready():
    print(f"{bot.user} has connected to Discord!")
    # Initialize matchmaking buttons
    await init_buttons()

    # Start timed tasks
    refresh_queue.start()
    refresh_api_data.start()


async def init_buttons():
    global mm_message
    # Initialize matchmaking buttons

    new_view = View(timeout=None)

    for i in range(len(mode_list)):
        button = Button(label=mode_list[i], style=ButtonStyle.blurple)

        async def press(interaction, mode=mode_list[i]):
            await interaction.response.defer()
            await enter_queue(interaction, mode)
            await interaction.followup.send("You have entered the " + mode + " queue.", ephemeral=True)

        button.callback = press
        new_view.add_item(button)

    dequeue_button = Button(label="Leave Queue", style=ButtonStyle.red)

    async def dequeue_press(interaction):
        await interaction.response.defer()
        await exit_queue(interaction)
        await interaction.followup.send("You have left the matchmaking queue.", ephemeral=True)

    dequeue_button.callback = dequeue_press

    feedback_button = Button(label="Give Feedback", style=ButtonStyle.url, url="https://forms.gle/KNKwp86VFxrgkZiW9")

    # button_view.add_item(stars_unranked_button)
    new_view.add_item(dequeue_button)
    new_view.add_item(feedback_button)
    channel = bot.get_channel(BUTTON_CHANNEL_ID)
    history = channel.history()
    async for m in history:
        if m.author == bot.user:
            await m.delete()

    mm_message = await channel.send("Matchmaking queue initialized! Press buttons below to search for a game.",
                                    view=new_view)


# Command for a player to enter the matchmaking queue
# If they are in the queue already, it will refresh their presence in the queue
# You can also move from one queue to another with this
# @bot.command(name="queue", aliases=["q"], help="Enter queue")
async def enter_queue(interaction, game_type="Superstars-Off Ranked"):
    player_rating = 1400
    player_id = str(interaction.user.id)
    player_name = interaction.user.name
    if game_type == "Superstars-On Ranked" or game_type == "Superstars-On Unranked":
        # TODO: Avoid accessing the API every time someone queues
        matches = on_log_sheet.findall(player_id)
        if matches:
            player_rating = round(float(on_log_sheet.cell(matches[-1].row, matches[-1].col + 3).value))
    else:
        # TODO: Avoid accessing the API every time someone queues
        matches = off_log_sheet.findall(player_id)
        if matches:
            player_rating = round(float(off_log_sheet.cell(matches[-1].row, matches[-1].col + 3).value))

    # put player in queue
    queue[player_id] = {"Name": player_name, "Rating": player_rating, "Time": time.time(), "Game Type": game_type}

    # calculate search range
    min_rating, max_rating = calc_search_range(player_rating, game_type, PERCENTILE_RANGE)

    # check for match
    await check_for_match(player_id, min_rating, max_rating, 0)

    await update_queue_status()


# Command for a player to remove themselves from the queue
# If they aren't in the queue, it will just post a message with the queue status
# @bot.command(name="dequeue", aliases=["dq"], help="Exit queue")
async def exit_queue(interaction):
    if str(interaction.user.id) in queue:
        del queue[str(interaction.user.id)]
    await update_queue_status()


@bot.command(name="ostat", help="Look up player batting stats on Project Rio")
async def o_stat(ctx, user="all", char="all"):
    url = "https://api.projectrio.app/detailed_stats/?exclude_pitching=1&exclude_fielding=1&exclude_misc=1&tag=Normal&tag=Ranked"
    all_url = url

    try:
        if char.lower() in characters.aliases:
            char = characters.mappings[characters.aliases[char.lower()]]
        if char != "all":
            url += "&char_id=" + str(characters.reverse_mappings[char])
            if user != "all":
                all_url = url

        if user != "all":
            url += "&username=" + user

        all_response = requests.get(all_url).json()
        response = requests.get(url).json()

        stats = response["Stats"]["Batting"]
        pa = stats["summary_at_bats"] + stats["summary_walks_bb"] + stats["summary_walks_hbp"] + stats[
            "summary_sac_flys"]
        avg = stats["summary_hits"] / stats["summary_at_bats"]
        obp = (stats["summary_hits"] + stats["summary_walks_hbp"] + stats["summary_walks_bb"]) / pa
        slg = (stats["summary_singles"] + (stats["summary_doubles"] * 2) + (stats["summary_triples"] * 3) + (
                stats["summary_homeruns"] * 4)) / stats["summary_at_bats"]
        ops = obp + slg
        # pa = stats["plate_appearances"]

        overall = all_response["Stats"]["Batting"]
        overall_pa = overall["summary_at_bats"] + overall["summary_walks_bb"] + overall["summary_walks_hbp"] + overall[
            "summary_sac_flys"]
        overall_obp = (overall["summary_hits"] + overall["summary_walks_hbp"] + overall[
            "summary_walks_bb"]) / overall_pa
        overall_slg = (overall["summary_singles"] + (overall["summary_doubles"] * 2) + (
                overall["summary_triples"] * 3) + (
                               overall["summary_homeruns"] * 4)) / overall["summary_at_bats"]

        ops_plus = ((obp / overall_obp) + (slg / overall_slg) - 1) * 100

        c_o = " cOPS+"
        if char == "all" or user == "all":
            c_o = " OPS+"

        embed = discord.Embed(title=user + " - " + char + " (" + str(pa) + " PA): ", description="AVG: " + "{:.3f}".format(avg) + "\nOBP: " + "{:.3f}".format(
            obp) + "\nSLG: " + "{:.3f}".format(slg) + "\nOPS: " + "{:.3f}".format(ops) + "\n" + c_o + ": " + str(round(ops_plus)))

        embed.set_thumbnail(url=characters.images[char])

        await ctx.send(embed=embed)
    except JSONDecodeError:
        await ctx.send("JSON Error")
    except KeyError:
        await ctx.send("Key Error")


@bot.command(name="pstat", help="Look up player pitching stats on Project Rio")
async def p_stat(ctx, user="all", char="all"):
    url = "https://api.projectrio.app/detailed_stats/?exclude_batting=1&exclude_fielding=1&exclude_misc=1&tag=Normal&tag=Ranked"
    all_url = url

    try:
        if char.lower() in characters.aliases:
            char = characters.mappings[characters.aliases[char.lower()]]
        if char != "all":
            url += "&char_id=" + str(characters.reverse_mappings[char])
            if user != "all":
                all_url = url

        if user != "all":
            url += "&username=" + user

        all_response = requests.get(all_url).json()
        response = requests.get(url).json()

        stats = response["Stats"]["Pitching"]

        # batter avg vs pitcher
        d_avg = stats["hits_allowed"] / (stats["batters_faced"] - stats["walks_bb"] - stats["walks_hbp"])
        era = 9 * stats["runs_allowed"] / (stats["outs_pitched"] / 3)
        # strikeout percentage
        kp = (stats["strikeouts_pitched"] / stats["batters_faced"]) * 100

        ip = stats["outs_pitched"] // 3
        ip_str = str(ip + (0.1 * (stats["outs_pitched"] % 3)))

        overall = all_response["Stats"]["Pitching"]
        overall_era = 9 * overall["runs_allowed"] / (overall["outs_pitched"] / 3)
        # character ERA-
        cera_minus = (era / overall_era) * 100

        char_or_all = " cERA-"
        if char == "all" or user == "all":
            char_or_all = " ERA-"

        embed = discord.Embed(title=user + " - " + char + " (" + ip_str + " IP): ",
                              description="opp. AVG: " + "{:.3f}".format(d_avg) + "\nERA: " + "{:.2f}".format(era) +
                                          "\nK%: " + "{:.1f}".format(kp) + "\n" + char_or_all + ": " + str(round(cera_minus)))

        embed.set_thumbnail(url=characters.images[char])

        await ctx.send(embed=embed)
    except JSONDecodeError:
        await ctx.send("JSON Error")
    except KeyError:
        await ctx.send("Key Error")


# refresh to see if a match can now be created with players waiting in the queue
@tasks.loop(seconds=15)
async def refresh_queue():
    for player in queue:
        time_in_queue = time.time() - queue[player]["Time"]
        new_range = PERCENTILE_RANGE + (PERCENTILE_RANGE * time_in_queue / 180)
        min_rating, max_rating = calc_search_range(queue[player]["Rating"], queue[player]["Game Type"], new_range)
        if await check_for_match(player, min_rating, max_rating, 120):
            await update_queue_status()
            break


# update spreadsheet API data once per minute
@tasks.loop(minutes=1)
async def refresh_api_data():
    global stars_off_sheet, stars_on_sheet, off_log_sheet, on_log_sheet, off_rating_list, on_rating_list
    stars_off_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-OFF")
    stars_on_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("STARS-ON")
    off_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-OFF")
    on_log_sheet = client.open_by_key("1B03IEnfOo3pAG7wBIjDW6jIHP0CTzn7jQJuxlNJebgc").worksheet("Logs-ON")
    off_rating_list = sorted(list(map(int, stars_off_sheet.col_values(5)[1:])), reverse=True)
    on_rating_list = sorted(list(map(int, stars_on_sheet.col_values(5)[1:])), reverse=True)


# Update message with the current queue status
async def update_queue_status():
    global mm_message
    queue_numbers = {}
    for mode in mode_list:
        queue_numbers[mode] = 0
    for user in queue:
        queue_numbers[queue[user]["Game Type"]] += 1

    new_message = "There are " + str(len(queue)) + " users in the matchmaking queue ("
    for mode in mode_list:
        new_message += str(queue_numbers[mode]) + " " + mode + ", "
    new_message = new_message[:-2] + ")"
    # print(queue)
    await mm_message.edit(content=new_message)


# params: player's rating and what percentile you want your search range to cover
# return: min and max rating the player can match against
def calc_search_range(rating, game_type, percentile):
    if game_type == "Superstars-On Ranked" or game_type == "Superstars-On Unranked":
        rating_list_copy = on_rating_list.copy()
    else:
        rating_list_copy = off_rating_list.copy()
    if game_type != "Superstars-Off Ranked":
        percentile = percentile * 2
    rating_list_copy.append(rating)
    rating_list_copy.append(0)
    rating_list_copy.append(3000)
    pct_list = sorted(rating_list_copy, reverse=True)
    max_index = round(pct_list.index(rating) - (len(pct_list) * percentile))
    min_index = round(pct_list.index(rating) + (len(pct_list) * percentile))
    if max_index < 0:
        max_index = 0
    if min_index >= len(pct_list):
        min_index = len(pct_list) - 1

    max_rating = pct_list[max_index]
    min_rating = pct_list[min_index]

    return min_rating, max_rating


# Checks if there is an available match for a user.
# Uses their user_id, search range (min-max ratings), and the min time an opponent must be searching to be matched.
async def check_for_match(user_id, min_rating, max_rating, min_time):
    print("Player:", queue[user_id]["Name"], "Rating:", queue[user_id]["Rating"], "Time:",
          round(time.time() - queue[user_id]["Time"]), "Rating Range", min_rating, max_rating)
    channel = bot.get_channel(MATCH_CHANNEL_ID)
    if len(queue) >= 2:
        best_match = False
        for player in queue:
            if max_rating >= queue[player]["Rating"] >= min_rating and \
                    player != user_id and time.time() - queue[player]["Time"] > min_time and \
                    queue[player]["Game Type"] == queue[user_id]["Game Type"]:
                if not best_match or abs(queue[best_match]["Rating"] - queue[user_id]["Rating"]) > abs(
                        queue[player]["Rating"] - queue[user_id]["Rating"]):
                    best_match = player

        if best_match:
            global match_count
            await channel.send("We have a " + queue[user_id][
                "Game Type"] + " match! <@" + user_id + "> vs <@" + str(best_match) + ">. Find matches in <#" + str(BUTTON_CHANNEL_ID) + ">")
            try:
                logging.info(str(match_count) + " " + queue[user_id]["Game Type"] + " match: " + queue[user_id][
                    "Name"] + " " + str(queue[user_id]["Rating"]) + " vs " + queue[best_match]["Name"] + " " + str(
                    queue[best_match]["Rating"]))
            except KeyError:
                print("Double match")
            match_count += 1
            if best_match in queue:
                del queue[best_match]
            if user_id in queue:
                del queue[user_id]
            return True

    if 300 < time.time() - queue[user_id]["Time"] < 315:
        role_id = "<@&998791156794150943>"
        if queue[user_id]["Game Type"] == "Superstars-On Ranked":
            role_id = "<@&998791464630898808>"
        await channel.send("There is a player looking for a match in queue! " + role_id)

    if 900 < time.time() - queue[user_id]["Time"] < 915:
        user = await bot.fetch_user(user_id)
        await user.send("You have been in the queue for 15 minutes. Please leave the queue if you have found a match or are no longer looking.")

    return False


# run the bot
bot.run(TOKEN)
