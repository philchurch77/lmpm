"""Fixed Copleston self-review descriptor content.

These templates hold the standard wording from the school's "Growth Model Self
Review" forms. They are seeded into `SelfReviewItem` + `SelfReviewBullet` rows
per review (see `SelfReview.seed_items`). Kept here as module data, mirroring
the `DEFAULT_STANDARDS_GOAL` constant pattern in models.py. Per-school
variation can be layered on later without restructuring the models.

Each item is a (code, heading, bullets) tuple:
- `code`     — stable key used for ordering and the uniqueness constraint.
- `heading`  — short label (the teacher-standard name); blank for numbered rows.
- `bullets`  — tuple of individual descriptor statements, each scored
  Not Answered/1/2/3 in the self-review UI, with one shared Evidence field per
  item (group).
"""

# Teaching staff: 8 Teacher Standards + Part 2 + Part 3.
TEACHING_ITEMS = [
    (
        "TS1",
        "TS1. Set high expectations which inspire, motivate and challenge pupils",
        (
            "I ensure all students are following SLANT and providing SHAPE "
            "responses in my lessons and I follow school procedures when "
            "they are not.",
            "I meet my students at the door, ensuring they sit in the "
            "seating plan space allocated and immediately start the do now "
            "task.",
            "I model the behaviour and attitudes that we expect from my "
            "students, following STEPS at all times.",
        ),
    ),
    (
        "TS2",
        "TS2. Promote good progress and outcomes by pupils",
        (
            "I take responsibility for the outcomes and progress of my "
            "students and intervene rapidly where students are falling "
            "behind.",
            "I engage with all Wednesday afternoon CPD in PLT on "
            "Explaining and Modelling, and Practice and Retrieval, and "
            "implement this in my teaching.",
        ),
    ),
    (
        "TS3",
        "TS3. Demonstrate good subject and curriculum knowledge",
        (
            "I make use of my disaggregated CPD hours to develop my own "
            "subject knowledge and subject specific pedagogy.",
            "I engage with CPD on literacy and oracy and make use of this "
            "in my own teaching.",
            "Where appropriate, I embrace opportunities to independently "
            "develop my own subject knowledge and delivery, both in my own "
            "subject and others.",
        ),
    ),
    (
        "TS4",
        "TS4. Plan and teach well structured lessons",
        (
            "My lessons begin with a do now task that has students "
            "recalling prior learning across a range of topics.",
            "The main part of my lesson follows the I do, We do, You do "
            "phases of the learning journey.",
            "I regularly use Knowledge Organisers in my lessons, to model "
            "their use to students, and I ensure all students have them on "
            "their desk in my lessons.",
            "Model answers are an integral part of my teaching, whether "
            "using the visualizer or other methods.",
            "Homework is set for my classes at the appropriate frequency "
            "and of a good quality.",
            "Rewards are issued in line with policy at the end of every "
            "lesson and the reasons for them are clearly narrated.",
            "Students exit the classroom in an orderly fashion, controlled "
            "by me.",
        ),
    ),
    (
        "TS5",
        "TS5. Adapt teaching to respond to the strengths and needs of all pupils",
        (
            "I am aware of the needs of my students, and I adapt my "
            "teaching to take account of these.",
            "I scaffold my activities appropriately to ensure that all "
            "learners are making progress.",
            "I teach to the top and scaffold up ensuring all students have "
            "access to powerful knowledge.",
            "I employ specific strategies in my teaching to ensure that "
            "all students can access the curriculum.",
        ),
    ),
    (
        "TS6",
        "TS6. Make accurate and productive use of assessment",
        (
            "I engage in the CPD delivered on questioning and feedback in "
            "PLT, and embed the skills learned in my teaching.",
            "I follow the school policies on assessment and ensure data is "
            "accurate and timely.",
            "I have 2 book looks and 2 feedback reviews in place.",
            "My students respond to feedback and redraft/correct their "
            "work accordingly in every lesson.",
            "I circulate whilst students work independently in the you do "
            "phase and provide in the moment feedback in every lesson "
            "(verbal or written).",
            "Where students are all making common errors I provide whole "
            "class feedback and then re-teach as necessary.",
        ),
    ),
    (
        "TS7",
        "TS7. Manage behaviour effectively to ensure a good and safe learning "
        "environment",
        (
            "I engage in the CPD delivered on Behaviour and Relationships "
            "in PLT, and use the skills learnt to develop and maintain a "
            "high level of expectation in my classroom.",
            "I follow the school behaviour policy and enforce it in my "
            "classroom and around the school.",
            "I use praise, sanctions, and rewards effectively to motivate "
            "students, modify their behaviour and build relationships.",
            "Seating plans are in place for my classes and are reviewed "
            "regularly.",
            "I issue Satchel badges and 1 golden ticket every lesson.",
        ),
    ),
    (
        "TS8",
        "TS8. Fulfil wider professional responsibilities",
        (
            "I contribute positively to the wider life and ethos of the "
            "school.",
            "If appropriate to my role, I complete lesson drop ins and "
            "provide advice and feedback to colleagues in my "
            "department/team.",
            "I communicate with parents and carers, including but not "
            "limited to parent's evenings and report writing.",
            "I regularly contact parents to praise student performance and "
            "clearly narrate the reasons for the praise.",
        ),
    ),
    (
        "PART2",
        "Part 2: Personal and Professional Conduct",
        (
            "I treat pupils with dignity and respect at all times, and "
            "ensure that the appropriate boundaries between teacher and "
            "student are met.",
            "I recognize the absolute importance of safeguarding students "
            "wellbeing, and follow school protocols on reporting and "
            "disclosure.",
            "I always show tolerance and respect for the rights of "
            "others.",
            "I promote British values, and ensure that my personal beliefs "
            "are not expressed in inappropriate ways.",
            "I take advantage of CPD opportunities, such as the T & L "
            "bulletin, podcasts and webinars that are made available.",
        ),
    ),
    (
        "PART3",
        "Part 3: Copleston Ethos and Practices",
        (
            "I engage in the process of professional growth, listening to "
            "feedback and discussing it with colleagues and leaders in a "
            "constructive manner that embraces school and trust policies.",
            "Engage in the PLT department and whole school programme, "
            "leading sessions where appropriate and practice and refine "
            "techniques in my lessons.",
            "I complete a minimum of 9 hours CPD during the year outside "
            "of directed time to fulfill the requirements of the "
            "disaggregated PD days.",
            "I routinely apply the learning journey, SLANT, STEPS and "
            "SHAPE and make this clear to students.",
            "I embrace the coaching model and refine my practice because I "
            "am always striving to get better.",
        ),
    ),
]


# Support staff: 9 generic descriptors (no per-row heading).
SUPPORT_ITEMS = [
    ("1", "", ("Be aware of and comply with school policy and procedures.",)),
    (
        "2",
        "",
        (
            "Be aware of and support difference and ensure all students "
            "have equal access to opportunities to learn and develop.",
        ),
    ),
    ("3", "", ("Contribute to the overall ethos/work/aims of the school.",)),
    ("4", "", ("Attend and participate in regular meetings.",)),
    (
        "5",
        "",
        ("Participate in training and other learning activities as required.",),
    ),
    (
        "6",
        "",
        (
            "To be aware of confidential issues linked to "
            "home/student/teacher/school work and to keep confidences as "
            "appropriate.",
        ),
    ),
    (
        "7",
        "",
        (
            "Having due regard for personal health and safety in the "
            "course of their duties including risk assessing home visits "
            "and other out of school duties.",
        ),
    ),
    (
        "8",
        "",
        (
            "Having due regard for safeguarding and promoting the welfare "
            "of children and young people and to follow the child "
            "protection procedure adopted by the Trust.",
        ),
    ),
    (
        "9",
        "",
        (
            "Demonstrating an active commitment to their own professional "
            "development.",
        ),
    ),
]


# Shown alongside the teaching Upper Pay Range declaration (page 4).
UPR_DECLARATION_TEXT = (
    "I understand that being on the Upper Pay Range requires me to demonstrate "
    "high levels of competence across the teacher standards and to make a "
    "substantial and sustained contribution to the school."
)
