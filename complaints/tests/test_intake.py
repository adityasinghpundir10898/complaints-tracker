from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from complaints.models import Complainant, Platform
from complaints.services import intake
from complaints.services.documents import (
    Attachment,
    UploadError,
    extract,
    extract_text,
    read_upload,
    sniff_mime,
)
from complaints.tests.helpers import fake_attachment
from complaints.workflow import register_complaint
from core.models import Village

# Modelled on the layout of a real handwritten-style Hindi petition to the Deputy Commissioner:
# subject wrapped over two lines, no "name:" label, applicants listed in the signature block,
# phone numbers written as "98765 43210". All names and numbers here are invented.
HINDI_LETTER = """
सेवा में

                माननीय उपायुक्त महोदय,

                सिरसा।

विषय :-      वाक्या मौजा भट्टू कलां तहसील डबवाली जिला सिरसा गली नम्बर 06 को

             पक्का करने बाबत।

श्रीमान् जी,

          सविनय निवेदन है कि प्रार्थीगण गांव भट्टू कलां, गली नम्बर 06 तहसील
डबवाली जिला सिरसा के रहने वाले है और प्रार्थीगण की उक्त गली का निर्माण पहले
किया जा चुका है।

यह कि गली का निर्माण ना होने के कारण गली में पानी खड़ा रहता है। जिस कारण से आप
महोदय को प्रार्थना है कि प्रार्थीगण की गली का निर्माण करवाया जावे।

          आपकी अति कृपा होगी।
सिरसा/ 18/09/2026                                          प्रार्थीगण

                                      रामलाल पुत्र बुटा सिंह व सुरेश
                                      पुत्र श्री हाकम सिंह निवासीगण गांव
                                      भट्टू कलां तहसील डबवाली जिला सिरसा।
                                      मो. 98765 43210, 91234 56789
"""

ENGLISH_FORM = """
Grievance ID: CMW/2026/778899
Name: Sunita Devi
Mobile: +91 98765 43210
Village: Bhattu Kalan
Subject: Old age pension not received for six months
"""


# ------------------------------------------------------------------ pre-fill


def test_guess_fields_from_a_hindi_petition(office, village):
    guess = intake.guess_fields(HINDI_LETTER, office)
    assert guess.name == "रामलाल"
    assert guess.phone == "9876543210"
    assert guess.subject == (
        "वाक्या मौजा भट्टू कलां तहसील डबवाली जिला सिरसा गली नम्बर 06 को पक्का करने बाबत।"
    )
    assert guess.village_id == village.pk
    assert guess.village_text == ""
    # All applicants stay in the address; the second phone is kept, not lost.
    assert "सुरेश पुत्र श्री हाकम सिंह" in guess.address
    assert "गांव भट्टू कलां तहसील डबवाली जिला सिरसा।" in guess.address
    assert "98765" not in guess.address.split("(Other phone")[0]
    assert guess.address.endswith("(Other phone: 9123456789)")


def test_description_is_the_letter_body_without_greeting_and_closing(office, village):
    description = intake.guess_fields(HINDI_LETTER, office).description
    assert description.startswith("सविनय निवेदन है कि प्रार्थीगण गांव भट्टू कलां")
    assert description.endswith("प्रार्थीगण की गली का निर्माण करवाया जावे।")
    assert "श्रीमान्" not in description
    assert "आपकी अति कृपा" not in description
    assert "पुत्र" not in description  # the signature block is not part of the description
    assert "\n" not in description


def test_description_from_a_labelled_field(office):
    text = (
        "Subject : Delay in mutation.\n\nDescription : The mutation has been pending for eight\n"
        "months. I have visited the office many times.\n"
    )
    guess = intake.guess_fields(text, office)
    assert guess.description == (
        "The mutation has been pending for eight months. I have visited the office many times."
    )


def test_description_from_a_hindi_labelled_field(office):
    text = "विषय : पेंशन बाबत।\nविवरण :  मेरी पेंशन छह माह से बंद है।\nकोई सुनवाई नहीं हुई है।"
    assert intake.guess_fields(text, office).description == (
        "मेरी पेंशन छह माह से बंद है। कोई सुनवाई नहीं हुई है।"
    )


def test_description_of_an_english_letter_stops_at_the_closing(office):
    text = (
        "Subject: Road repair.\n\nRespected Sir,\n\nThe road is damaged since six months.\n"
        "Please repair it.\n\nYours faithfully,\nName: Rajinder Singh\nVillage: Ratia"
    )
    assert intake.guess_fields(text, office).description == (
        "The road is damaged since six months. Please repair it."
    )


def test_description_stops_at_the_name_and_phone_fields_when_there_is_no_closing(office):
    text = (
        "Subject: Blocked drain.\n\nRespected Sir,\n\nThe drain is blocked for a month.\n\n"
        "Name: Baljeet Singh\nMobile: 98555 66778\nWhatsApp chat\nBaljeet: it is blocked"
    )
    assert intake.guess_fields(text, office).description == "The drain is blocked for a month."


def test_description_without_a_subject_label_starts_after_the_greeting(office):
    text = "To,\nThe SDM\n\nRespected Sir,\nThe road is damaged.\nThanking you"
    assert intake.guess_fields(text, office).description == "The road is damaged."


def test_description_is_empty_when_the_text_has_no_structure(office):
    assert intake.guess_fields("lorem ipsum dolor", office).description == ""


def test_ocr_pipe_in_place_of_danda_still_ends_the_subject(office):
    text = (
        "विषय :- सड़क की मरम्मत बाबत |\n\nश्रीमान् जी,\n\n"
        "सविनय निवेदन है कि सड़क टूटी है।\n\nआपकी अति कृपा होगी |"
    )
    guess = intake.guess_fields(text, office)
    assert guess.subject == "सड़क की मरम्मत बाबत |"
    assert guess.description == "सविनय निवेदन है कि सड़क टूटी है।"


def test_guess_fields_from_a_labelled_english_form(office, village):
    guess = intake.guess_fields(ENGLISH_FORM, office)
    assert guess.name == "Sunita Devi"
    assert guess.phone == "9876543210"
    assert guess.external_ref == "CMW/2026/778899"
    assert guess.subject == "Old age pension not received for six months"
    assert guess.village_id == village.pk


def test_village_not_in_the_list_is_reported_not_guessed(office, village):
    guess = intake.guess_fields(HINDI_LETTER.replace("भट्टू कलां", "नौरंग"), office)
    assert guess.village_id is None
    assert guess.village_text == "नौरंग"


def test_village_found_in_the_text_without_a_label(office, village):
    guess = intake.guess_fields("Resident of Bhattu Kalan, near the school", office)
    assert guess.village_id == village.pk


def test_longest_village_name_wins_when_scanning(office, village):
    short = Village.objects.create(office=office, name="Bhattu")
    guess = intake.guess_fields("Resident of Bhattu Kalan", office)
    assert guess.village_id == village.pk
    assert short.pk != guess.village_id


def test_subject_wrapped_over_lines_stops_at_the_salutation(office):
    text = "Subject: Request for road repair in the\nvillage\n\nSir,\nI request you to ..."
    assert intake.guess_fields(text, office).subject == "Request for road repair in the village"


def test_subject_stops_after_three_lines_when_no_end_is_found(office):
    text = "विषय :- एक\nदो\nतीन\nचार\nपांच"
    assert intake.guess_fields(text, office).subject == "एक दो तीन"


def test_signature_block_with_relation_marker_in_english(office):
    text = "Yours faithfully\nApplicant\nSh. Ramesh Kumar S/o Mohan Lal R/o Tohana\nMob. 9876543210"
    guess = intake.guess_fields(text, office)
    assert guess.name == "Ramesh Kumar"
    assert guess.phone == "9876543210"


def test_labels_inside_the_body_are_not_mistaken_for_a_signature(office):
    text = "The applicants state that the road is broken and ask for repair.\nThank you."
    guess = intake.guess_fields(text, office)
    assert (guess.name, guess.address) == ("", "")


def test_phone_numbers_come_from_the_english_ocr_pass_when_hindi_ocr_garbles_digits(office):
    # Real OCR of a Hindi letter gave "9306 7685" for 93061 76815 and a wrong digit in the
    # second number; the English-only pass read both correctly. These numbers are invented.
    hindi_ocr = "प्रार्थीगण\nरामलाल पुत्र बुटा सिंह निवासी गांव तोहाना\nमो. 9876 4321, 91234 56780"
    latin_ocr = "Mo 98765 43210, 91234 56789"
    guess = intake.guess_fields(hindi_ocr, office, latin_ocr)
    assert guess.phone == "9876543210"
    assert guess.address == "रामलाल पुत्र बुटा सिंह निवासी गांव तोहाना (Other phone: 9123456789)"


def test_misread_phone_line_is_dropped_from_the_address_when_nothing_better_exists(office):
    text = "प्रार्थीगण\nरामलाल पुत्र बुटा सिंह निवासी गांव तोहाना।\nमो. 9876 4321, 9123 45678"
    guess = intake.guess_fields(text, office)
    assert guess.address == "रामलाल पुत्र बुटा सिंह निवासी गांव तोहाना।"
    assert guess.phone == ""


def test_label_junk_from_ocr_is_stripped_from_the_subject(office):
    text = "विषय :-. वाक्या मौजा तोहाना गली को\nपक्का करने बाबत |\nश्रीमान जी,"
    assert intake.guess_fields(text, office).subject == "वाक्या मौजा तोहाना गली को पक्का करने बाबत |"


def test_village_hint_ignores_a_garbled_word_the_letter_does_not_repeat(office):
    text = (
        "विषय :- वाक्या मौजा aka तहसील डबवाली\n"
        "सविनय निवेदन है कि प्रार्थगण गांव नौरंग, गली नम्बर 06 तहसील डबवाली\n"
        "प्रार्थीगण\nरामलाल पुत्र बुटा सिंह निवासीगण गांव\nनौरंग तहसील डबवाली जिला सिरसा।"
    )
    guess = intake.guess_fields(text, office)
    assert guess.village_id is None and guess.village_text == "नौरंग"


def test_signature_label_spelled_without_the_long_vowel_is_still_found(office):
    text = "आपकी अति कृपा होगी।\nप्रार्थगण\nरामलाल पुत्र बुटा सिंह निवासी गांव तोहाना"
    assert intake.guess_fields(text, office).name == "रामलाल"


def test_handwritten_date_and_misread_phone_lines_are_kept_out_of_the_address(office):
    # Real OCR put the handwritten date line inside the signature block. Invented values.
    hindi_ocr = (
        "प्रार्थीगण\n"
        "रामलाल पुत्र बुटा सिंह व सुरेश सिंह\n"
        "सिरसा / |8 | ७१ | १-०७०.६\n"
        "पुत्र श्री हाकम सिंह निवासीगण गांव\n"
        "तोहाना तहसील डबवाली जिला सिरसा |\n"
        "मो. 9876। 4321, 91234 56780"
    )
    guess = intake.guess_fields(hindi_ocr, office, "Al. 98765 43210, 91234 56789")
    assert guess.name == "रामलाल"
    assert guess.phone == "9876543210"
    assert guess.address == (
        "रामलाल पुत्र बुटा सिंह व सुरेश सिंह पुत्र श्री हाकम सिंह निवासीगण गांव "
        "तोहाना तहसील डबवाली जिला सिरसा | (Other phone: 9123456789)"
    )


def test_an_address_line_with_a_few_digits_is_not_treated_as_noise(office):
    text = "प्रार्थीगण\nरामलाल पुत्र बुटा सिंह निवासी मकान नम्बर 123, गली 4\nतोहाना जिला सिरसा"
    assert "मकान नम्बर 123, गली 4" in intake.guess_fields(text, office).address


def test_hindi_labelled_form_gives_the_name(office):
    text = (
        "शिकायत प्रपत्र\nशिकायतकर्ता का नाम :  सरोज देवी\nमोबाइल नम्बर :  98123 45670\n"
        "गांव :  तोहाना\nविषय :  पेंशन नहीं मिलने बाबत।"
    )
    guess = intake.guess_fields(text, office, "Mo 98123 45670")
    assert guess.name == "सरोज देवी"
    assert guess.phone == "9812345670"
    assert guess.subject == "पेंशन नहीं मिलने बाबत।"


def test_a_village_name_label_is_not_mistaken_for_the_person(office):
    assert intake.guess_fields("गांव का नाम : तोहाना\nविषय : सड़क।", office).name == ""


def test_zero_width_joiners_from_ocr_do_not_break_matching(office, village):
    text = (
        "विषय :- भट्\u200cटू कलां की नाली बाबत।\nप्रार्थीगण\nरामलाल पुत्र बुटा सिंह निवासी गांव भट्\u200cटू कलां"
    )
    guess = intake.guess_fields(text, office)
    assert guess.subject == "भट्टू कलां की नाली बाबत।"
    assert guess.village_id == village.pk


def test_ocr_text_has_zero_width_joiners_removed(tools):
    tools("\f", hindi="भट्\u200cटू कलां", hindi_conf=90)
    assert extract_text(b"%PDF-1.4", "application/pdf")[0] == "भट्टू कलां"


def test_guess_fields_on_empty_or_unreadable_text_suggests_nothing(office):
    assert intake.guess_fields("", office) == intake.Prefill()
    assert intake.guess_fields("lorem ipsum", office) == intake.Prefill()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("98765 43210", "9876543210"),
        ("+91 98765-43210", "9876543210"),
        ("091234 56789", "9123456789"),
        ("९८७६५ ४३२१०", "9876543210"),
        ("", ""),
    ],
)
def test_normalise_phone(raw, expected):
    assert intake.normalise_phone(raw) == expected


def test_devanagari_digits_in_the_letter_are_read(office):
    text = "प्रार्थीगण\nरामलाल पुत्र बुटा सिंह निवासी गांव तोहाना\nमो. ९८७६५ ४३२१०"
    assert intake.guess_fields(text, office).phone == "9876543210"


# -------------------------------------------------------------- duplicates


def test_duplicate_by_external_ref_phone_and_similar_subject(office, make_complaint, village):
    person = Complainant.objects.create(office=office, name="A", phone="9000011111")
    existing = make_complaint(
        complainant=person,
        external_ref="CMW-1",
        subject="Encroachment on village common land",
        village=village,
    )

    by_ref = intake.find_possible_duplicates(office, external_ref="cmw-1")
    by_phone = intake.find_possible_duplicates(office, phone="9000011111")
    by_subject = intake.find_possible_duplicates(
        office, subject="Encroachment on the village common land", village=village
    )
    assert [c for c, _ in by_ref] == [existing]
    assert [c for c, _ in by_phone] == [existing]
    assert [c for c, _ in by_subject] == [existing]
    assert "same external reference" in by_ref[0][1]


def test_similar_subject_in_a_different_village_is_not_flagged(office, make_complaint, village):
    make_complaint(subject="Encroachment on village common land", village=village)
    elsewhere = Village.objects.create(office=office, name="Tohana")
    assert (
        intake.find_possible_duplicates(
            office, subject="Encroachment on village common land", village=elsewhere
        )
        == []
    )


def test_no_duplicates_when_nothing_matches(office, make_complaint):
    make_complaint(subject="Something else entirely")
    assert intake.find_possible_duplicates(office, external_ref="X", phone="9111111111") == []


def test_duplicates_are_limited_to_the_same_office(office, make_complaint):
    from core.models import Office

    make_complaint(external_ref="SHARED")
    other = Office.objects.create(name="Other", code="OTH", district="d", state="s")
    assert intake.find_possible_duplicates(other, external_ref="SHARED") == []


# ---------------------------------------------------------------- uploads


def test_sniff_mime_uses_file_content_not_the_name():
    assert sniff_mime(b"%PDF-1.7 ...") == "application/pdf"
    assert sniff_mime(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert sniff_mime(b"MZ\x90\x00 an exe") is None


def test_read_upload_rejects_wrong_type_even_with_pdf_name():
    upload = SimpleUploadedFile("evil.pdf", b"MZ\x90\x00 not a pdf", content_type="application/pdf")
    with pytest.raises(UploadError):
        read_upload(upload)


def test_read_upload_rejects_big_files(settings):
    settings.MAX_UPLOAD_BYTES = 10
    upload = SimpleUploadedFile("a.pdf", b"%PDF-1.4 more than ten bytes")
    with pytest.raises(UploadError):
        read_upload(upload)


def test_read_upload_keeps_only_the_file_name():
    upload = SimpleUploadedFile("../../etc/passwd.pdf", b"%PDF-1.4 ok")
    assert read_upload(upload).filename == "passwd.pdf"


def test_staged_upload_roundtrip_and_hash_validation(media_root):
    attachment = fake_attachment("staged")
    sha = intake.stage_upload(attachment)
    loaded = intake.load_staged(sha, {"filename": "staged.pdf", "extracted_text": "hello"})
    assert loaded.content == attachment.content
    assert loaded.extracted_text == "hello"
    assert intake.load_staged("../../secret", {}) is None
    assert intake.load_staged("a" * 64, {}) is None


# ------------------------------------------------------------ text extraction


TSV_HEADER = "\t".join(
    "level page_num block_num par_num line_num word_num left top width height conf text".split()
)


def tsv(text: str, conf: float) -> str:
    rows = [TSV_HEADER]
    for line_no, line in enumerate(text.splitlines(), 1):
        for word_no, word in enumerate(line.split(), 1):
            rows.append(f"5\t1\t1\t1\t{line_no}\t{word_no}\t0\t0\t0\t0\t{conf}\t{word}")
    return "\n".join(rows)


@pytest.fixture
def tools(monkeypatch):
    """Pretend pdftotext, pdftoppm and tesseract exist and record how they are called."""
    calls: list[list[str]] = []
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def install(
        pdftotext_output: str,
        hindi: str = "Scanned words",
        latin: str = "",
        hindi_conf: float = 90,
        latin_conf: float = 20,
    ):
        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[0] == "pdftotext":
                return subprocess.CompletedProcess(args, 0, stdout=pdftotext_output)
            if args[0] == "pdftoppm":
                Path(args[-1] + ".png").write_bytes(b"png")
                return subprocess.CompletedProcess(args, 0, stdout="")
            assert args[0] == "tesseract" and args[-1] == "tsv"
            lang = args[args.index("-l") + 1]
            out = tsv(hindi, hindi_conf) if lang == "hin" else tsv(latin, latin_conf)
            return subprocess.CompletedProcess(args, 0, stdout=out)

        monkeypatch.setattr("complaints.services.documents.subprocess.run", fake_run)
        return calls

    return install


def test_extract_text_without_tools_leaves_status_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert extract_text(b"%PDF-1.4", "application/pdf") == ("", "none", None)


def test_extract_text_reads_the_text_layer_without_ocr(tools):
    calls = tools("A typed complaint with enough text.\f")
    assert extract_text(b"%PDF-1.4", "application/pdf") == (
        "A typed complaint with enough text.",
        "done",
        1,
    )
    assert [c[0] for c in calls] == ["pdftotext"]


def test_extract_text_ocrs_a_fully_scanned_pdf(tools):
    calls = tools("\f")
    assert extract_text(b"%PDF-1.4", "application/pdf") == ("Scanned words", "done", 1)
    assert [c[0] for c in calls] == ["pdftotext", "pdftoppm", "tesseract", "tesseract"]
    assert calls[1][calls[1].index("-r") + 1] == "300"


def test_each_language_is_run_alone_never_as_hin_plus_eng(tools):
    calls = tools("\f")
    extract_text(b"%PDF-1.4", "application/pdf")
    langs = [c[c.index("-l") + 1] for c in calls if c[0] == "tesseract"]
    assert langs == ["hin", "eng"]


def test_the_more_confident_language_becomes_the_text(tools):
    tools("\f", hindi="junk glyphs", latin="Please repair the road", hindi_conf=35, latin_conf=92)
    assert extract_text(b"%PDF-1.4", "application/pdf")[0] == "Please repair the road"


def test_a_hindi_page_keeps_its_hindi_text_and_the_english_pass_is_kept_for_digits(tools):
    tools("\f", hindi="मो. 9306 7685", latin="Mo 93061 76815", hindi_conf=88, latin_conf=40)
    result = extract(b"%PDF-1.4", "application/pdf")
    assert result.text == "मो. 9306 7685"
    assert result.latin_text == "Mo 93061 76815"
    assert result.status == "done" and result.pages == 1


def test_pages_with_a_text_layer_need_no_english_pass(tools):
    tools("A typed complaint with enough text.\f")
    assert extract(b"%PDF-1.4", "application/pdf").latin_text == ""


def test_extract_text_ocrs_only_the_image_pages_of_a_mixed_pdf(tools):
    # Page 1 typed, page 2 a pasted screenshot, page 3 typed again.
    calls = tools("First typed page of the complaint.\f\fThird typed page, also long enough.\f")
    text, status, pages = extract_text(b"%PDF-1.4", "application/pdf")
    assert status == "done" and pages == 3
    assert text.splitlines() == [
        "First typed page of the complaint.",
        "Scanned words",
        "Third typed page, also long enough.",
    ]
    ocr_pages = [c[c.index("-f") + 1] for c in calls if c[0] == "pdftoppm"]
    assert ocr_pages == ["2"]


def test_extract_text_stops_ocr_after_the_page_budget(tools, settings):
    settings.INTAKE_OCR_MAX_PAGES = 2
    calls = tools("\f" * 5)
    text, status, pages = extract_text(b"%PDF-1.4", "application/pdf")
    assert pages == 5
    assert sum(1 for c in calls if c[0] == "pdftoppm") == 2
    assert text.count("Scanned words") == 2


def test_extract_text_without_ocr_tools_keeps_the_typed_pages(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/pdftotext" if name == "pdftotext" else None
    )
    monkeypatch.setattr(
        "complaints.services.documents.subprocess.run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 0, stdout="A typed page with enough words.\f\f"
        ),
    )
    assert extract_text(b"%PDF-1.4", "application/pdf") == (
        "A typed page with enough words.",
        "done",
        2,
    )


def test_a_failing_ocr_page_does_not_lose_the_other_pages(monkeypatch, tools):
    tools("A typed page with enough words.\f\f")

    def flaky(args, **kwargs):
        if args[0] == "pdftotext":
            return subprocess.CompletedProcess(
                args, 0, stdout="A typed page with enough words.\f\f"
            )
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr("complaints.services.documents.subprocess.run", flaky)
    assert extract_text(b"%PDF-1.4", "application/pdf") == (
        "A typed page with enough words.",
        "done",
        2,
    )


def test_extract_text_of_a_photo_uses_tesseract(tools):
    calls = tools("", hindi="Text in a photo")
    assert extract_text(b"\xff\xd8\xff", "image/jpeg") == ("Text in a photo", "done", 1)
    assert [c[0] for c in calls] == ["tesseract", "tesseract"]


def test_extract_text_when_nothing_is_readable_reports_none(tools):
    tools("\f", hindi="", latin="")
    assert extract_text(b"%PDF-1.4", "application/pdf") == ("", "none", 1)


def test_extract_text_failure_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    def boom(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr("complaints.services.documents.subprocess.run", boom)
    assert extract_text(b"%PDF-1.4", "application/pdf") == ("", "failed", None)


def test_register_with_staged_attachment_end_to_end(office, clerk, media_root):
    attachment = fake_attachment("intake")
    sha = intake.stage_upload(attachment)
    staged = intake.load_staged(sha, {"filename": "intake.pdf"})
    person = Complainant.objects.create(office=office, name="X")
    c = register_complaint(
        office=office,
        complainant=person,
        actor=clerk,
        source_platform=Platform.INTERNAL,
        received_on=date(2026, 9, 1),
        subject="s",
        attachment=staged,
    )
    assert c.documents.get().sha256 == attachment.sha256
    assert isinstance(staged, Attachment)
