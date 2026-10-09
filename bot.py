import os
import random
import string
import asyncio
import json
import hmac
import hashlib
import secrets
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

import discord
import asyncpg
import aiohttp
from aiohttp import web
from discord.ext import commands


# =========================================================
# ENVIRONMENT VARIABLES
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

SELLAUTH_API_KEY = os.getenv("SELLAUTH_API_KEY")
SELLAUTH_SHOP_ID = os.getenv("SELLAUTH_SHOP_ID")
SELLAUTH_WEBHOOK_SECRET = os.getenv("SELLAUTH_WEBHOOK_SECRET")

PORT = int(os.getenv("PORT", "8080"))

# Required for sending email verification codes. Configure in Railway Variables.
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
EMAIL_CODE_SECRET = os.getenv("EMAIL_CODE_SECRET", "")



# =========================================================
# DISCORD SETUP
# =========================================================

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

db_pool = None


# =========================================================
# REFERRAL CODE GENERATOR
# =========================================================

def generate_code():
    characters = string.ascii_uppercase + string.digits

    random_part = "".join(
        random.choices(characters, k=5)
    )

    return f"ACE-{random_part}"


# =========================================================
# SELLAUTH API
# =========================================================

async def create_sellauth_coupon(code):

    url = (
        f"https://api.sellauth.com/v1/shops/"
        f"{SELLAUTH_SHOP_ID}/coupons"
    )

    headers = {
        "Authorization": f"Bearer {SELLAUTH_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "code": code,
        "global": True,
        "discount": 10,
        "type": "percentage",
        "max_uses_per_customer": 1,
        "min_invoice_price": 0,
        "disable_if_volume_discount": False,
        "disable_if_quantity_deal": False,
        "disable_if_bundle_offer": False,
        "disable_if_gateway_discount": False,
        "disable_if_affiliate_discount": True,
        "uses_count_mode": "paid",
        "applies_to_renewals": False
    }

    async with aiohttp.ClientSession() as session:

        async with session.post(
            url,
            json=payload,
            headers=headers
        ) as response:

            response_text = await response.text()

            if response.status not in (200, 201):

                print(
                    "SellAuth coupon creation failed "
                    f"({response.status}): "
                    f"{response_text}"
                )

                return False

            print(
                f"SellAuth coupon created: {code}"
            )

            return True


async def get_sellauth_invoice(invoice_id):

    url = (
        f"https://api.sellauth.com/v1/shops/"
        f"{SELLAUTH_SHOP_ID}/invoices/"
        f"{invoice_id}"
    )

    headers = {
        "Authorization": f"Bearer {SELLAUTH_API_KEY}",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:

        async with session.get(
            url,
            headers=headers
        ) as response:

            response_text = await response.text()

            if response.status != 200:

                print(
                    "Failed to retrieve SellAuth invoice "
                    f"{invoice_id} ({response.status}): "
                    f"{response_text}"
                )

                return None

            return json.loads(response_text)


# =========================================================
# DATABASE
# =========================================================

async def setup_database():

    global db_pool

    db_pool = await asyncpg.create_pool(
        DATABASE_URL
    )

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                discord_id BIGINT PRIMARY KEY,
                referral_code VARCHAR(20)
                    UNIQUE NOT NULL,
                referral_count INTEGER
                    DEFAULT 0,
                sales_generated NUMERIC(10, 2)
                    DEFAULT 0,
                reward_balance NUMERIC(10, 2)
                    DEFAULT 0,
                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS
            processed_referral_invoices (
                invoice_id VARCHAR(100)
                    PRIMARY KEY,
                referral_code VARCHAR(20)
                    NOT NULL,
                sale_amount NUMERIC(10, 2)
                    DEFAULT 0,
                processed_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    # Additional tables do not alter existing referral records.
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_emails (
                discord_id BIGINT PRIMARY KEY,
                email TEXT NOT NULL,
                verified BOOLEAN NOT NULL DEFAULT FALSE,
                code_hash TEXT,
                code_expires_at TIMESTAMPTZ,
                verified_at TIMESTAMPTZ,
                last_sent_at TIMESTAMPTZ
            );
        """)

    print(
        "Database connected and referral tables ready!"
    )


async def get_referral(discord_id):

    async with db_pool.acquire() as conn:

        return await conn.fetchrow(
            """
            SELECT *
            FROM referrals
            WHERE discord_id = $1
            """,
            discord_id
        )


async def create_referral(discord_id):

    async with db_pool.acquire() as conn:

        existing = await conn.fetchrow(
            """
            SELECT *
            FROM referrals
            WHERE discord_id = $1
            """,
            discord_id
        )

        if existing:
            return existing

        while True:

            code = generate_code()

            code_exists = await conn.fetchval(
                """
                SELECT 1
                FROM referrals
                WHERE referral_code = $1
                """,
                code
            )

            if code_exists:
                continue

            coupon_created = (
                await create_sellauth_coupon(code)
            )

            if coupon_created:
                break

        return await conn.fetchrow(
            """
            INSERT INTO referrals (
                discord_id,
                referral_code
            )
            VALUES ($1, $2)
            RETURNING *;
            """,
            discord_id,
            code
        )


# =========================================================
# COUPON EXTRACTION
# =========================================================

def extract_coupon_code(invoice):

    coupon = invoice.get("coupon")

    if not coupon:
        return None

    if isinstance(coupon, dict):

        code = coupon.get("code")

        if code:
            return str(code).upper()

    if isinstance(coupon, str):
        return coupon.upper()

    return None


def extract_buyer_email(invoice):
    """Best-effort extraction; confirm exact fields with your SellAuth payload."""
    for record in (invoice, invoice.get("customer"), invoice.get("buyer")):
        if isinstance(record, dict):
            for key in ("email", "customer_email", "buyer_email"):
                value = record.get(key)
                if isinstance(value, str) and "@" in value:
                    return value.strip().casefold()
    return None


# =========================================================
# PROCESS REFERRAL SALE
# =========================================================

async def process_referral_invoice(invoice_id):

    invoice = await get_sellauth_invoice(
        invoice_id
    )

    if not invoice:
        return

    if str(invoice.get("shop_id")) != str(
        SELLAUTH_SHOP_ID
    ):

        print(
            f"Ignoring invoice {invoice_id}: "
            "wrong shop."
        )

        return

    if invoice.get("status") != "completed":

        print(
            f"Ignoring invoice {invoice_id}: "
            f"status={invoice.get('status')}"
        )

        return

    coupon_code = extract_coupon_code(
        invoice
    )

    if not coupon_code:

        print(
            f"Invoice {invoice_id} has no coupon."
        )

        return

    if not coupon_code.startswith("ACE-"):

        print(
            f"Invoice {invoice_id} used "
            f"non-referral coupon {coupon_code}."
        )

        return

    try:

        sale_amount = float(
            invoice.get("paid_usd")
            or invoice.get("price_usd")
            or 0
        )

    except (TypeError, ValueError):

        sale_amount = 0.0

    async with db_pool.acquire() as conn:

        async with conn.transaction():

            referral = await conn.fetchrow(
                """
                SELECT *
                FROM referrals
                WHERE UPPER(referral_code) = $1
                FOR UPDATE
                """,
                coupon_code
            )

            if not referral:

                print(
                    f"No referral owner found "
                    f"for {coupon_code}."
                )

                return

            # Never award credit for a verified account's own checkout email.
            # If SellAuth doesn't return a usable buyer email, fail closed:
            # do not award an unverified referral.
            buyer_email = extract_buyer_email(invoice)
            owner_email = await conn.fetchrow(
                "SELECT email, verified FROM referral_emails WHERE discord_id = $1",
                referral["discord_id"]
            )
            if not owner_email or not owner_email["verified"]:
                print(f"Referral {coupon_code} not credited: owner email unverified")
                return
            if not buyer_email:
                print(f"Referral {coupon_code} not credited: invoice buyer email unavailable")
                return
            if buyer_email.casefold() == owner_email["email"].casefold():
                print(f"Self-referral blocked for {coupon_code}")
                return

            inserted = await conn.fetchval(
                """
                INSERT INTO processed_referral_invoices (
                    invoice_id,
                    referral_code,
                    sale_amount
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (invoice_id)
                DO NOTHING
                RETURNING invoice_id;
                """,
                str(invoice_id),
                coupon_code,
                sale_amount
            )

            if not inserted:

                print(
                    f"Invoice {invoice_id} "
                    "was already credited."
                )

                return

            await conn.execute(
                """
                UPDATE referrals
                SET
                    referral_count =
                        referral_count + 1,
                    sales_generated =
                        sales_generated + $1
                WHERE discord_id = $2;
                """,
                sale_amount,
                referral["discord_id"]
            )

    print(
        f"Referral credited: {coupon_code} | "
        f"${sale_amount:.2f} | "
        f"Invoice {invoice_id}"
    )

    try:

        user = bot.get_user(
            referral["discord_id"]
        )

        if user is None:

            user = await bot.fetch_user(
                referral["discord_id"]
            )

        embed = discord.Embed(
            title="🎉 New Successful Referral!",
            description=(
                "Someone completed a purchase "
                "using your Ace Services "
                "referral code!"
            )
        )

        embed.add_field(
            name="🎟️ Code",
            value=f"`{coupon_code}`",
            inline=False
        )

        embed.add_field(
            name="💰 Sale",
            value=f"${sale_amount:.2f}",
            inline=True
        )

        embed.add_field(
            name="👥 Referral",
            value="+1",
            inline=True
        )

        await user.send(
            embed=embed
        )

    except Exception as error:

        print(
            "Could not DM referral owner: "
            f"{error}"
        )


# =========================================================
# SELLAUTH WEBHOOK SIGNATURE
# =========================================================

def verify_sellauth_signature(
    raw_body,
    received_signature
):

    if not received_signature:
        return False

    expected_signature = hmac.new(
        SELLAUTH_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        expected_signature,
        received_signature
    )


# =========================================================
# WEBHOOK SERVER
# =========================================================

async def health_check(request):

    return web.json_response(
        {
            "status": "online",
            "service": "Ace Referral Bot"
        }
    )


async def sellauth_webhook(request):

    try:

        raw_body = await request.read()

        signature = request.headers.get(
            "X-Signature"
        )

        if not verify_sellauth_signature(
            raw_body,
            signature
        ):

            print(
                "Rejected SellAuth webhook: "
                "invalid signature."
            )

            return web.Response(
                status=401,
                text="Invalid signature"
            )

        try:

            payload = json.loads(
                raw_body.decode("utf-8")
            )

        except json.JSONDecodeError:

            return web.Response(
                status=400,
                text="Invalid JSON"
            )

        event = payload.get("event")
        shop_id = payload.get("shop_id")
        data = payload.get("data", {})

        print(
            f"SellAuth webhook received: {event}"
        )

        if str(shop_id) != str(
            SELLAUTH_SHOP_ID
        ):

            return web.Response(
                status=403,
                text="Wrong shop"
            )

        if (
            event ==
            "NOTIFICATION.SHOP_INVOICE_PROCESSED"
        ):

            invoice_id = data.get(
                "invoice_id"
            )

            if invoice_id:

                asyncio.create_task(
                    process_referral_invoice(
                        invoice_id
                    )
                )

        return web.Response(
            status=200,
            text="OK"
        )

    except Exception as error:

        print(
            f"Webhook error: {error}"
        )

        return web.Response(
            status=500,
            text="Internal error"
        )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health_check
    )

    app.router.add_post(
        "/webhook/sellauth",
        sellauth_webhook
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"Webhook server listening "
        f"on port {PORT}"
    )

    return runner


# =========================================================
# REFERRAL BUTTON
# =========================================================

class ReferralView(discord.ui.View):

    def __init__(self):

        super().__init__(
            timeout=None
        )

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

        try:

            await interaction.response.defer(
                ephemeral=True
            )

            referral = await create_referral(
                interaction.user.id
            )

            code = referral[
                "referral_code"
            ]

            embed = discord.Embed(
                title="🎉 Your Ace Referral Code",
                description=(
                    "Your personal referral "
                    "code is:\n\n"
                    f"## `{code}`\n\n"
                    "Share this code with someone "
                    "purchasing from "
                    "**Ace Services**.\n\n"
                    "🏷️ They receive **10% off** "
                    "when using your code.\n"
                    "💰 Completed purchases count "
                    "toward your referrals.\n\n"
                    "**Your code is permanently "
                    "linked to your account.**"
                )
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True
            )

        except Exception as error:

            print(
                f"Referral button error: {error}"
            )

            try:

                await interaction.followup.send(
                    "❌ Something went wrong while "
                    "generating your referral code.",
                    ephemeral=True
                )

            except Exception:
                pass


# =========================================================
# MEMBER REFERRAL STATS
# =========================================================

@bot.command()
async def referrals(ctx):

    referral = await get_referral(
        ctx.author.id
    )

    if not referral:

        await ctx.reply(
            "You haven't generated a "
            "referral code yet!"
        )

        return

    embed = discord.Embed(
        title="💎 Your Ace Referral Stats"
    )

    embed.add_field(
        name="🎟️ Referral Code",
        value=(
            f"`{referral['referral_code']}`"
        ),
        inline=False
    )

    embed.add_field(
        name="👥 Successful Referrals",
        value=str(
            referral["referral_count"]
        ),
        inline=True
    )

    embed.add_field(
        name="💰 Sales Generated",
        value=(
            f"${float(referral['sales_generated']):.2f}"
        ),
        inline=True
    )

    embed.add_field(
        name="🎁 Reward Balance",
        value=(
            f"${float(referral['reward_balance']):.2f}"
        ),
        inline=True
    )

    await ctx.reply(
        embed=embed
    )


# =========================================================
# OWNER REFERRAL STATS
# =========================================================

@bot.command()
@commands.is_owner()
async def referralstats(
    ctx,
    member: discord.Member = None
):

    if member is None:

        await ctx.reply(
            "❌ Mention a member.\n\n"
            "Example: `!referralstats @user`"
        )

        return

    referral = await get_referral(
        member.id
    )

    if not referral:

        embed = discord.Embed(
            title="❌ No Referral Account",
            description=(
                f"{member.mention} has not "
                "generated a referral code yet."
            )
        )

        await ctx.reply(
            embed=embed
        )

        return

    created_at = referral[
        "created_at"
    ]

    if created_at:

        created_text = (
            created_at.strftime(
                "%B %d, %Y"
            )
        )

    else:

        created_text = "Unknown"

    embed = discord.Embed(
        title="👑 Ace Referral Monitor",
        description=(
            f"Referral statistics for "
            f"{member.mention}"
        )
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.add_field(
        name="👤 Member",
        value=(
            f"{member}\n"
            f"`{member.id}`"
        ),
        inline=False
    )

    embed.add_field(
        name="🎟️ Referral Code",
        value=(
            f"`{referral['referral_code']}`"
        ),
        inline=False
    )

    embed.add_field(
        name="👥 Successful Referrals",
        value=str(
            referral["referral_count"]
        ),
        inline=True
    )

    embed.add_field(
        name="💰 Sales Generated",
        value=(
            f"${float(referral['sales_generated']):.2f}"
        ),
        inline=True
    )

    embed.add_field(
        name="🎁 Reward Balance",
        value=(
            f"${float(referral['reward_balance']):.2f}"
        ),
        inline=True
    )

    embed.add_field(
        name="📅 Referral Member Since",
        value=created_text,
        inline=False
    )

    await ctx.reply(
        embed=embed
    )


# =========================================================
# OWNER REFERRAL CODE LOOKUP
# =========================================================

@bot.command()
@commands.is_owner()
async def lookup(ctx, code: str = None):
    if not code:
        await ctx.reply("❌ Usage: `!lookup ACE-XXXXX`")
        return

    code = code.strip().upper()

    async with db_pool.acquire() as conn:
        referral = await conn.fetchrow(
            """
            SELECT * FROM referrals
            WHERE UPPER(referral_code) = $1
            """,
            code
        )

    if not referral:
        await ctx.reply(f"❌ No referral owner found for `{code}`.")
        return

    discord_id = referral["discord_id"]
    try:
        user = await bot.fetch_user(discord_id)
        owner_text = f"{user.mention} ({user})"
    except discord.DiscordException:
        owner_text = f"Unknown user (`{discord_id}`)"

    embed = discord.Embed(
        title="🔎 Ace Referral Code Lookup",
        color=discord.Color.blue()
    )
    embed.add_field(name="🎟️ Referral Code", value=f"`{referral['referral_code']}`", inline=False)
    embed.add_field(name="👤 Code Owner", value=owner_text, inline=False)
    embed.add_field(name="🆔 Discord ID", value=f"`{discord_id}`", inline=False)
    embed.add_field(name="👥 Successful Referrals", value=str(referral["referral_count"]), inline=True)
    embed.add_field(name="💰 Sales Generated", value=f"${float(referral['sales_generated']):.2f}", inline=True)
    created_at = referral["created_at"]
    embed.add_field(
        name="📅 Code Created",
        value=created_at.strftime("%B %d, %Y") if created_at else "Unknown",
        inline=False
    )
    await ctx.reply(embed=embed)


@lookup.error
async def lookup_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.reply("⛔ Only the bot owner can use `!lookup`.", mention_author=False)
    else:
        raise error


# =========================================================
# PRIVATE EMAIL VERIFICATION AND OWNER CHECK
# =========================================================

def email_code_hash(discord_id, code):
    return hmac.new(
        EMAIL_CODE_SECRET.encode(), f"{discord_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def send_verification_email(destination, code):
    msg = EmailMessage()
    msg["Subject"] = "Ace Services email verification"
    msg["From"] = SMTP_FROM
    msg["To"] = destination
    msg.set_content(
        f"Your Ace Services verification code is {code}. "
        "It expires in 10 minutes. If you didn't request this, ignore it."
    )
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


@bot.command()
@commands.dm_only()
async def setmail(ctx, email: str = None):
    """Register an email by DM. Never send addresses in public channels."""
    if not email or len(email) > 254 or email.count("@") != 1 or " " in email:
        await ctx.reply("Usage (DM only): `!setmail your@email.com`")
        return
    if not all((SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, EMAIL_CODE_SECRET)):
        await ctx.reply("Email verification is not configured yet. Contact the bot owner.")
        return
    email = email.strip().casefold()
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT email, verified, last_sent_at FROM referral_emails WHERE discord_id=$1",
            ctx.author.id
        )
        now = datetime.now(timezone.utc)
        if existing and existing["verified"] and existing["email"] == email:
            await ctx.reply("This email is already verified.")
            return
        if existing and existing["last_sent_at"] and now - existing["last_sent_at"] < timedelta(minutes=2):
            await ctx.reply("Please wait two minutes before requesting another code.")
            return
        code = f"{secrets.randbelow(1000000):06d}"
        # Send first; only save pending state if delivery succeeds.
        try:
            await asyncio.to_thread(send_verification_email, email, code)
        except Exception:
            await ctx.reply("Couldn't send the verification email. Contact the bot owner.")
            return
        await conn.execute("""
            INSERT INTO referral_emails
                (discord_id,email,verified,code_hash,code_expires_at,verified_at,last_sent_at)
            VALUES ($1,$2,FALSE,$3,$4,NULL,$5)
            ON CONFLICT(discord_id) DO UPDATE SET
                email=EXCLUDED.email, verified=FALSE,
                code_hash=EXCLUDED.code_hash,
                code_expires_at=EXCLUDED.code_expires_at,
                verified_at=NULL, last_sent_at=EXCLUDED.last_sent_at
        """, ctx.author.id, email, email_code_hash(ctx.author.id, code),
             now + timedelta(minutes=10), now)
    await ctx.reply("Verification email sent. Reply here with `!verifymail 123456`.")


@bot.command()
@commands.dm_only()
async def verifymail(ctx, code: str = None):
    if not code or len(code) != 6 or not code.isascii() or not code.isdigit():
        await ctx.reply("Usage (DM only): `!verifymail 123456`")
        return
    if not EMAIL_CODE_SECRET:
        await ctx.reply("Verification is not configured.")
        return
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM referral_emails WHERE discord_id=$1", ctx.author.id
        )
        if not row or not row["code_hash"] or not row["code_expires_at"]:
            await ctx.reply("No pending verification. Use `!setmail` first.")
            return
        if datetime.now(timezone.utc) >= row["code_expires_at"]:
            await ctx.reply("Code expired. Use `!setmail` to request another.")
            return
        if not hmac.compare_digest(row["code_hash"], email_code_hash(ctx.author.id, code)):
            await ctx.reply("Incorrect verification code.")
            return
        await conn.execute("""
            UPDATE referral_emails
            SET verified=TRUE, verified_at=NOW(), code_hash=NULL, code_expires_at=NULL
            WHERE discord_id=$1
        """, ctx.author.id)
    await ctx.reply("✅ Your email is verified. Your eligible referrals can now earn credit.")


@bot.command()
@commands.is_owner()
async def checkmail(ctx, member: discord.User = None):
    """Only the application owner can request a member's registered email."""
    if member is None:
        await ctx.reply("Usage: `!checkmail @user` (run in your server)")
        return
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email, verified FROM referral_emails WHERE discord_id=$1", member.id
        )
    if not row:
        report = f"Member: {member} ({member.id})\nEmail: Not registered"
    else:
        report = (f"Member: {member} ({member.id})\n"
                  f"Full registered email: {row['email']}\n"
                  f"Verified: {'Yes' if row['verified'] else 'No'}")
    report += ("\nIP address: Unavailable. Discord does not provide member IPs; "
               "checkout IPs cannot be reliably attributed to a Discord account.")
    try:
        await ctx.author.send(report)
        if ctx.guild:
            await ctx.reply("📩 Private email report sent to your DMs.", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("Enable DMs from the bot to receive the private report.", mention_author=False)


@checkmail.error
async def checkmail_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.reply("⛔ Only the bot owner can use `!checkmail`.", mention_author=False)
    else:
        raise error


# =========================================================
# REFERRAL PANEL
# =========================================================

@bot.command()
@commands.has_permissions(
    administrator=True
)
async def referralpanel(ctx):

    embed = discord.Embed(
        title="💎 ACE SERVICES REFERRAL PROGRAM",
        description=(
            "Invite people to "
            "**Ace Services** and earn rewards!\n\n"
            "🎁 Click below to generate your "
            "personal referral code.\n\n"
            "🏷️ Your referrals receive "
            "**10% off** their purchase.\n"
            "💰 Completed purchases count "
            "toward your referral stats.\n"
            "🔒 Every member receives one "
            "permanent referral code.\n\n"
            "**Click below to get started!**"
        )
    )

    await ctx.send(
        embed=embed,
        view=ReferralView()
    )


# =========================================================
# BOT EVENTS
# =========================================================

@bot.event
async def on_ready():

    print(
        f"Logged in as {bot.user}"
    )

    print(
        "Ace Referral Bot is online!"
    )


# =========================================================
# START EVERYTHING
# =========================================================

async def main():

    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing."
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is missing."
        )

    if not SELLAUTH_API_KEY:
        raise RuntimeError(
            "SELLAUTH_API_KEY is missing."
        )

    if not SELLAUTH_SHOP_ID:
        raise RuntimeError(
            "SELLAUTH_SHOP_ID is missing."
        )

    if not SELLAUTH_WEBHOOK_SECRET:
        raise RuntimeError(
            "SELLAUTH_WEBHOOK_SECRET is missing."
        )

    await setup_database()

    bot.add_view(
        ReferralView()
    )

    await start_web_server()

    await bot.start(
        TOKEN
    )




@setmail.error
async def setmail_error(ctx, error):
    if isinstance(error, commands.PrivateMessageOnly):
        await ctx.reply("Please DM me `!setmail your@email.com` instead of posting your email here.", mention_author=False)
    else:
        raise error


@verifymail.error
async def verifymail_error(ctx, error):
    if isinstance(error, commands.PrivateMessageOnly):
        await ctx.reply("Please DM me your verification code instead.", mention_author=False)
    else:
        raise error


if __name__ == "__main__":

    asyncio.run(
        main()
    )
