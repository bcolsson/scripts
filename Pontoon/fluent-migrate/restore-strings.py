#!/usr/bin/env python3
import argparse
import os
from itertools import tee
from compare_locales.parser import getParser
from compare_locales.serializer import serialize

def pairwise(iterable):
    a, b = tee(iterable) 
    next(b, None) 
    return zip(a, b)

def migrate_files(source_locale, locale, base_path, restore_string_path, filename, restoration_strings):
    restoration_source_file_path = os.path.join(restore_string_path, locale, filename)
    restoration_source_file_parser = getParser(restoration_source_file_path)
    restoration_source_file_parser.readFile(restoration_source_file_path)

    restoration_entities = []

    for entity, next_entity in pairwise(list(restoration_source_file_parser.walk())):
        if f"{entity}" in restoration_strings:
            restoration_entities.append(entity)
            restoration_entities.append(next_entity)

    
    restoration_target_file_path = os.path.join(base_path, locale, filename)
    restoration_target_file_parser = getParser(restoration_target_file_path)
    restoration_target_file_parser.readFile(restoration_target_file_path)

    output = [
        entity
        for entity in list(restoration_target_file_parser.walk())
    ]
    
    source_locale_file_path = os.path.join(base_path, source_locale, filename)
    source_locale_file_parser = getParser(source_locale_file_path)
    source_locale_file_parser.readFile(source_locale_file_path)
    source_reference = list(source_locale_file_parser.walk())

    for entry in restoration_entities:
        source_reference.append(entry)
        output.append(entry)

    output_data = serialize(restoration_target_file_path, source_reference, output, {})

    os.makedirs(os.path.dirname(restoration_target_file_path), exist_ok=True)
    with open(restoration_target_file_path, "wb") as f:
        f.write(output_data)
    
    return restoration_target_file_path

def process_terms(restoration_terms):
    terms = []
    for term in restoration_terms:
        terms.append(f"-{term}")
    return terms

def main():
    arguments = argparse.ArgumentParser()
    arguments.add_argument(
        "--source-locale",
        required=True,
        dest="source_locale",
        help="Source locale code",
    )
    arguments.add_argument(
        "--ignore-locale",
        nargs="*",
        required=False,
        dest="ignore_locales",
        help="Ignore locales",
    )
    arguments.add_argument(
        "--restoration-strings",
        nargs="*",
        required=False,
        dest="restoration_strings",
        help="String ids to restore",
    )
    arguments.add_argument(
        "--restoration-terms",
        nargs="*",
        required=False,
        dest="restoration_terms",
        help="String ids to restore",
    )
    arguments.add_argument(
        "--base-path",
        required=True,
        dest="base_path",
        help="Path to folder including subfolders for all locales.",
    )
    arguments.add_argument(
        "--file",
        required=True,
        dest="filename",
        help="Filename.",
    )
    arguments.add_argument(
        "--locales",
        nargs="*",
        required=False,
        dest="locales",
        help="Locales to process")
    arguments.add_argument(
        "--restoration-string-path",
        nargs="?",
        required=True,
        dest="restore_string_path",
        help="Path to folder of previous translations to be restored"
    )

    args = arguments.parse_args()
    if args.restoration_strings is None and args.restoration_terms is None:
        arguments.error("Must define at least 1 string or term")
    source_locale = args.source_locale

    # Get a list of files to update (absolute paths)
    base_path = os.path.realpath(args.base_path)
    filename = args.filename
    restore_string_path = os.path.realpath(args.restore_string_path)
            
    if args.restoration_strings and args.restoration_terms:
        restoration_strings = list(args.restoration_strings)
        restoration_strings.append(process_terms(args.restoration_terms))
    elif args.restoration_strings:
        restoration_strings = list(args.restoration_strings)
    elif args.restoration_terms:
        restoration_strings = process_terms(args.restoration_terms)

    if args.locales:
        locales = args.locales
    else:
        locales = [
            d
            for d in os.listdir(base_path)
            if os.path.isdir(os.path.join(base_path, d)) and not d.startswith(".")
        ]
        locales.remove(source_locale)
    if args.ignore_locales:
        for locale in args.ignore_locales:
            if locale in locales:
                locales.remove(locale)
    locales.sort()
    files_written = []
    
    for locale in locales:
        try:
            files_written.append(migrate_files(source_locale, locale, base_path, restore_string_path, filename, restoration_strings))
        except FileNotFoundError:
            print(f"Warning: {filename} not found for {locale}")
    output_files_written = list(set(files_written))
    output_files_written.sort()
    print("Files written:")
    for output_file in output_files_written:
        print(output_file)


if __name__ == "__main__":
    main()
