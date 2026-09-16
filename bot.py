import os
import random
import string
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Temporary storage.
# We'll replace this with a permanent database after testing.
referral_codes = {}


def generate_code():
    characters = string.ascii_uppercase + string.digits
    random_part = "".join(random.choices(characters, k=5))
    return f"ACE-{random_part}"


class ReferralView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Generate Referral Code",
        style=discord.ButtonStyle.green,
        emoji="🎁",
        custom_id="ace_generate_referral"
    )
    async def generate_referral(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        user_id = interaction.user.id

        # If they already have a code, show the existing one
        if user_id in referral_codes:
            code = referral_codes[user_id]

            embed = discord.Embed(
                title="🎁 Your Ace Referral Code",
                description=(
                    f"Your referral code is:\n\n"
                    f"**`{code}`**\n\n"
                    "Share this code with someone purchasing from "
                    "**Ace Services**."
                )
            )

            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )
            return

        # Generate a unique code
        while True:
            code = generate_code()

            if code not in referral_codes.values():
                break

        referral_codes[user_id] = code

        embed = discord.Embed(
            title="🎉 Referral Code Created!",
            description=(
                f"Your personal referral code is:\n\n"
                f"## `{code}`\n\n"
                "Share this code with anyone purchasing from "
                "**Ace Services**.\n\n"
                "🏷️ When they use your code, they'll receive a discount.\n"
                "💰 Their completed purchase will count toward your referrals."
            )
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )


@bot.event
async def on_ready():
    bot.add_view(ReferralView())

    print(f"Logged in as {bot.user}")
    print("Ace Referral Bot is online!")


@bot.command()
@commands.has_permissions(administrator=True)
async def referralpanel(ctx):
    embed = discord.Embed(
        title="💎 ACE SERVICES REFERRAL PROGRAM",
        description=(
            "Invite people to **Ace Services** and earn rewards!\n\n"
            "🎁 Click the button below to generate your personal "
            "referral code.\n\n"
            "🏷️ Your referrals receive a discount on their purchase.\n"
            "💰 Completed purchases count toward your referral rewards.\n"
            "🔒 Every member receives one unique referral code.\n\n"
            "**Click below to get started!**"
        )
    )

    await ctx.send(embed=embed, view=ReferralView())


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

bot.run(TOKEN)
